"""
Targets: anything in the sky you might point a camera at.

- Presets: popular astrophotography targets with friendly names (offline).
- Catalog: all ~13,400 NGC, IC, Messier, Caldwell and addendum objects from OpenNGC (offline).
- Planets and the Moon: computed by astropy (offline).
- Anything else catalogued (stars, Sharpless, LDN, PGC, HD, quasars…): CDS Sesame → SIMBAD/NED/VizieR.
- Comets, asteroids and Pluto: JPL Horizons, with positions computed for your site.
- Raw coordinates: "10h41m +41d16m", "10:41:00 +41:16:00" or "160.25 41.27".

Target ids are self-describing so a forecast URL always works: "M31", "NGC891", "body:mars",
"coord:344.2746,62.6359;Sh2-155", "jpl:C/2023 A3".
"""
import gzip
import json
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

import requests

from . import horizons

CATALOG_FILE = Path(__file__).resolve().parent / "data" / "openngc.json.gz"
SESAME_URL = "https://cds.unistra.fr/cgi-bin/nph-sesame/-oI/SNV?"

# Popular targets, J2000 RA/Dec in degrees. Ids double as catalog ids where they exist.
PRESETS = [
    ("none", "No target (wide-field / sky only)", "Any", None, None),
    ("M31", "M31 Andromeda Galaxy", "Galaxy", 10.6847, 41.2690),
    ("M33", "M33 Triangulum Galaxy", "Galaxy", 23.4621, 30.6599),
    ("M51", "M51 Whirlpool Galaxy", "Galaxy", 202.4696, 47.1952),
    ("M81", "M81 Bode's Galaxy", "Galaxy", 148.8882, 69.0653),
    ("M82", "M82 Cigar Galaxy", "Galaxy", 148.9685, 69.6797),
    ("M101", "M101 Pinwheel Galaxy", "Galaxy", 210.8024, 54.3488),
    ("M104", "M104 Sombrero Galaxy", "Galaxy", 189.9976, -11.6231),
    ("M42", "M42 Orion Nebula", "Emission nebula", 83.8221, -5.3911),
    ("B33", "Horsehead Nebula", "Emission nebula", 85.2458, -2.4583),
    ("NGC2237", "Rosette Nebula", "Emission nebula", 97.9792, 4.9417),
    ("NGC7000", "North America Nebula", "Emission nebula", 314.6833, 44.3333),
    ("IC1396", "Elephant's Trunk (IC 1396)", "Emission nebula", 324.7417, 57.5000),
    ("IC1805", "Heart Nebula", "Emission nebula", 38.1750, 61.4500),
    ("IC1848", "Soul Nebula", "Emission nebula", 42.7875, 60.4333),
    ("M8", "M8 Lagoon Nebula", "Emission nebula", 270.9042, -24.3867),
    ("M16", "M16 Eagle Nebula", "Emission nebula", 274.7000, -13.8067),
    ("M17", "M17 Omega Nebula", "Emission nebula", 275.1958, -16.1717),
    ("M20", "M20 Trifid Nebula", "Emission nebula", 270.6208, -22.9717),
    ("NGC6960", "Veil Nebula (West)", "Supernova remnant", 311.4292, 30.7167),
    ("M1", "M1 Crab Nebula", "Supernova remnant", 83.6331, 22.0145),
    ("M27", "M27 Dumbbell Nebula", "Planetary nebula", 299.9016, 22.7211),
    ("M57", "M57 Ring Nebula", "Planetary nebula", 283.3963, 33.0292),
    ("NGC7293", "Helix Nebula", "Planetary nebula", 337.4108, -20.8372),
    ("M45", "M45 Pleiades", "Star cluster", 56.7500, 24.1167),
    ("M13", "M13 Hercules Cluster", "Star cluster", 250.4235, 36.4613),
    ("NGC869", "Double Cluster", "Star cluster", 35.0833, 57.1333),
    ("M44", "M44 Beehive Cluster", "Star cluster", 130.1000, 19.6667),
    ("GC", "Milky Way core", "Milky Way", 266.4168, -29.0078),
    ("NGC253", "Sculptor Galaxy", "Galaxy", 11.8880, -25.2883),
    ("LMC", "Large Magellanic Cloud", "Galaxy", 80.8942, -69.7561),
    ("SMC", "Small Magellanic Cloud", "Galaxy", 13.1867, -72.8286),
    ("NGC5139", "Omega Centauri", "Star cluster", 201.6970, -47.4795),
    ("NGC3372", "Carina Nebula", "Emission nebula", 161.2650, -59.8667),
]
BODIES = ["moon", "mercury", "venus", "mars", "jupiter", "saturn", "uranus", "neptune"]

# SIMBAD object-type codes → our types (anything unlisted becomes "Object").
_SIMBAD_TYPES = {
    "HII": "Emission nebula", "EmO": "Emission nebula", "DNe": "Dark nebula", "RNe": "Reflection nebula",
    "PN": "Planetary nebula", "SNR": "Supernova remnant", "OpC": "Open cluster", "Cl*": "Open cluster",
    "GlC": "Globular cluster", "ClG": "Galaxy cluster", "GrG": "Galaxy group", "ISM": "Nebula", "Cld": "Nebula",
    "MoC": "Nebula", "GNe": "Nebula", "G": "Galaxy", "AGN": "Galaxy", "SyG": "Galaxy", "Sy1": "Galaxy",
    "Sy2": "Galaxy", "H2G": "Galaxy", "SBG": "Galaxy", "LSB": "Galaxy", "EmG": "Galaxy", "rG": "Galaxy",
    "QSO": "Quasar", "BLL": "Quasar", "Bla": "Quasar",
}


def _norm(text):
    return re.sub(r"[\s_\-·.']+", "", str(text)).lower()


def _base(**kw):
    t = {"id": None, "name": None, "short": None, "type": "Object", "kind": "fixed", "ra": None, "dec": None,
         "mode": "deep", "size": None, "mag": None, "const": None, "source": None}
    t.update(kw)
    t["short"] = t["short"] or t["name"]
    return t


def _preset(row):
    tid, name, kind, ra, dec = row
    short = tid if re.match(r"^M\d+$", tid) else name
    if tid == "none":
        return _base(id="none", name=name, short=name, type="Any", kind="none", source="preset")
    return _base(id=tid, name=name, short=short, type=kind, ra=ra, dec=dec, source="preset")


def _body(name):
    label = "Moon" if name == "moon" else name.capitalize()
    return _base(id=f"body:{name}", name=label, short=label, type="Moon" if name == "moon" else "Planet",
                 kind="body", mode="planetary", source="astropy")


PRESET_BY_ID = {row[0]: _preset(row) for row in PRESETS}


@lru_cache(maxsize=1)
def catalog():
    """(objects by id, alias index) from the bundled OpenNGC file."""
    with gzip.open(CATALOG_FILE, "rt", encoding="utf-8") as f:
        raw = json.load(f)["objects"]
    by_id, index = {}, {}
    for oid, common, kind, ra, dec, size, mag, const, aliases in raw:
        name = f"{oid} {common}" if common else oid
        t = _base(id=oid, name=name, short=oid if not common or re.match(r"^M\d+$", oid) else common,
                  type=kind, ra=ra, dec=dec, size=size, mag=mag, const=const, source="OpenNGC")
        t["aliases"] = aliases
        t["common"] = common
        by_id[oid] = t
        for a in [oid, *aliases, common]:
            if a:
                index.setdefault(_norm(a), oid)
    return by_id, index


def as_list():
    """The quick-pick list: presets, then the planets and the Moon."""
    return [PRESET_BY_ID[r[0]] for r in PRESETS] + [_body(b) for b in BODIES]


def _public(t):
    return {k: t[k] for k in ("id", "name", "short", "type", "kind", "mode", "ra", "dec", "size", "mag", "const", "source")}


def get(target_id):
    """Target dict for an id. Raises ValueError for anything it can't interpret."""
    tid = (target_id or "none").strip()
    if tid in PRESET_BY_ID:
        return PRESET_BY_ID[tid]
    if tid.startswith("body:") and tid[5:] in BODIES:
        return _body(tid[5:])
    if tid.startswith("coord:"):
        coords, _, label = tid[6:].partition(";")
        label, _, kind = label.partition("|type=")
        try:
            ra, dec = (float(x) for x in coords.split(","))
        except ValueError:
            raise ValueError(f"Bad coordinates in target '{tid}'")
        if not (0 <= ra < 360 and -90 <= dec <= 90):
            raise ValueError("RA must be 0–360° and Dec −90…+90°")
        label = label or f"RA {ra:.3f}° Dec {dec:+.3f}°"
        return _base(id=tid, name=label, type=kind or "Object", ra=ra, dec=dec, source="coordinates")
    if tid.startswith("jpl:"):
        query = tid[4:]
        command = horizons.command_for(query)
        kind = "Dwarf planet" if command == horizons.MAJOR["pluto"] else "Comet" if re.match(r"^(DES=)?(\d+[PI]|[PCDXI]/)", command) else "Asteroid / comet"
        return _base(id=tid, name=query, type=kind, kind="track", source="JPL Horizons", command=command)
    by_id, index = catalog()
    hit = by_id.get(tid) or by_id.get(index.get(_norm(tid), ""))
    if hit:
        return _public(hit)
    raise ValueError(f"Unknown target '{tid}'. Search for it by name, or enter coordinates.")


def search(q, limit=15):
    """Offline matches for the search box: presets, bodies and the catalog, best first."""
    nq = _norm(q)
    if not nq:
        return [_public(t) for t in as_list()[1:limit + 1]]
    hits = []
    for t in as_list():
        names = [t["id"], t["name"], t["short"]]
        rank = _rank(nq, names)
        if rank is not None:
            hits.append((rank, 0, 0, t))
    by_id, _ = catalog()
    seen = {h[3]["id"] for h in hits}
    for t in by_id.values():
        if t["id"] in seen:
            continue
        rank = _rank(nq, [t["id"], *t["aliases"], t["common"]])
        if rank is not None:
            # Brighter, Messier, then bigger objects first among equal matches.
            hits.append((rank, 0 if t["id"].startswith("M") else 1, t["mag"] if t["mag"] is not None else 99, t))
    hits.sort(key=lambda h: h[:3])
    return [_public(h[3]) for h in hits[:limit]]


def _rank(nq, names):
    best = None
    for name in names:
        if not name:
            continue
        n = _norm(name)
        r = 0 if n == nq else 1 if n.startswith(nq) else 2 if any(_norm(w).startswith(nq) for w in str(name).split()) else 3 if nq in n and len(nq) >= 3 else None
        if r is not None and (best is None or r < best):
            best = r
    return best


def _parse_coords(q):
    """RA/Dec in degrees from typed coordinates, or None."""
    from astropy import units as u
    from astropy.coordinates import SkyCoord

    text = q.strip().replace(",", " ")
    nums = re.fullmatch(r"\s*([0-9.]+)\s+([+-]?[0-9.]+)\s*", text)
    try:
        if nums:
            c = SkyCoord(float(nums.group(1)) * u.deg, float(nums.group(2)) * u.deg)
        elif re.search(r"\d", text) and re.search(r"[hms:]", text) and len(text.split()) >= 2:
            c = SkyCoord(text, unit=(u.hourangle, u.deg))
        else:
            return None
    except (ValueError, TypeError):
        return None
    return round(float(c.ra.deg), 5), round(float(c.dec.deg), 5)


def _sesame(q):
    try:
        res = requests.get(SESAME_URL + quote(q.strip()), timeout=12)
        res.raise_for_status()
    except requests.RequestException:
        return None
    ra = dec = otype = main = None
    for line in res.text.splitlines():
        if line.startswith("%J ") and ra is None:
            parts = line.split()
            ra, dec = float(parts[1]), float(parts[2])
        elif line.startswith("%C.0") and otype is None:
            otype = line.split(maxsplit=1)[1].strip()
        elif line.startswith("%I.0") and main is None:
            main = re.sub(r"\s+", " ", line[4:].strip())
    if ra is None:
        return None
    kind = _SIMBAD_TYPES.get(otype or "", "Star" if otype and "*" in otype else "Object")
    return ra, dec, kind, main


def resolve(q):
    """
    Turn anything typed into a target, going online if needed. Returns
    {"target": {...}} or {"error": "...", "matches": [{"query", "label"}]}.
    """
    text = (q or "").strip()
    if not text:
        return {"error": "Type an object name, catalog number or coordinates."}
    if _norm(text) in ("sun", "thesun", "sol"):
        return {"error": "The Sun isn't a night target, and needs a certified solar filter to image safely."}
    local = search(text, limit=1)
    if local:
        entry = catalog()[0].get(local[0]["id"], {})
        rank = _rank(_norm(text), [local[0]["id"], local[0]["name"], local[0]["short"], *entry.get("aliases", []), entry.get("common")])
        if rank is not None and rank <= 2:  # exact, prefix or a whole-word match ("Whirlpool")
            return {"target": local[0]}
    for b in BODIES:
        if _norm(text) == b:
            return {"target": _public(_body(b))}
    coords = _parse_coords(text)
    if coords:
        return {"target": _public(get(f"coord:{coords[0]},{coords[1]}"))}
    found = _sesame(text)
    if found:
        ra, dec, kind, main = found
        label = text if _norm(text) == _norm(main or "") else f"{text} ({main})" if main else text
        t = get(f"coord:{ra:.5f},{dec:.5f};{label}|type={kind}")
        t = {**_public(t), "name": label, "short": label, "type": kind, "source": "SIMBAD (CDS Sesame)"}
        return {"target": t}
    try:
        command, name = horizons.resolve(text)
    except horizons.HorizonsError as err:
        if err.matches:
            return {"error": str(err), "matches": err.matches}
        if local:
            return {"target": local[0]}
        return {"error": f"Couldn't find “{text}” in the catalog, SIMBAD or JPL Horizons. Try a catalog number or coordinates."}
    t = _public(get(f"jpl:{text}"))
    return {"target": {**t, "name": name, "short": name}}

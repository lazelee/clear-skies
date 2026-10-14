"""
Place search for anywhere on Earth, from two complementary geocoders queried in parallel:

- Open-Meteo (GeoNames): ranks by population, so a bare "Paris" or "Montréal" finds the big city,
  and returns the time zone and elevation directly.
- Photon (OpenStreetMap): understands free text with regions ("Scarborough, ON"), suburbs and
  merged former towns, small villages and names in any script ("東京", "Москва").

Anything after a comma ("Gravenhurst, Ontario", "Springfield, IL, USA") is a region filter.
Results that don't match it are never shown in its place.
"""
import json
import math
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path

import requests

GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
PHOTON_URL = "https://photon.komoot.io/api/"
USER_AGENT = "clear-skies/1.0 (local astrophotography planner; https://github.com/lazelee/clear-skies)"

# OSM place types worth offering as a location (not shops, stations or buildings).
_PLACE_VALUES = {"city", "town", "village", "hamlet", "suburb", "neighbourhood", "quarter", "borough",
                 "municipality", "locality", "island", "islet", "isolated_dwelling", "county", "province",
                 "state", "region", "district", "archipelago"}

# Region shorthand people type after a comma: US states, Canadian provinces, Australian states, countries.
_REGION_ABBR = {
    **{k.lower(): v for k, v in {
        "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
        "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
        "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
        "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
        "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
        "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
        "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
        "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
        "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
        "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
        "AB": "Alberta", "BC": "British Columbia", "MB": "Manitoba", "NB": "New Brunswick",
        "NL": "Newfoundland and Labrador", "NS": "Nova Scotia", "NT": "Northwest Territories", "NU": "Nunavut",
        "ON": "Ontario", "PE": "Prince Edward Island", "QC": "Quebec", "SK": "Saskatchewan", "YT": "Yukon",
        "NSW": "New South Wales", "QLD": "Queensland", "VIC": "Victoria", "TAS": "Tasmania",
        "SA": "South Australia", "ACT": "Australian Capital Territory",
    }.items()},
    # Codes shared between countries.
    "wa": ("Washington", "Western Australia"), "nt": ("Northwest Territories", "Northern Territory"),
    "england": "England", "scotland": "Scotland", "wales": "Wales",
}

# ISO country code → English name (GeoNames countryInfo, CC-BY 4.0), plus names people commonly
# type that differ from it. A qualifier that names a country matches by code, however it's spelled.
COUNTRIES = {k: v.strip() for k, v in json.loads(
    (Path(__file__).resolve().parent / "data" / "countries.json").read_text(encoding="utf-8")).items()}
_COUNTRY_ALIASES = {
    "usa": "US", "us": "US", "america": "US", "united states of america": "US",
    "uk": "GB", "britain": "GB", "great britain": "GB", "united kingdom of great britain and northern ireland": "GB",
    "uae": "AE", "nz": "NZ", "aus": "AU",
    "dr congo": "CD", "drc": "CD", "congo kinshasa": "CD", "democratic republic of congo": "CD",
    "congo brazzaville": "CG", "republic of congo": "CG", "congo": "CG",
    "cote d'ivoire": "CI", "cote divoire": "CI", "czech republic": "CZ", "czechia": "CZ", "burma": "MM",
    "holland": "NL", "the netherlands": "NL", "korea": "KR", "republic of korea": "KR", "east timor": "TL",
    "timor-leste": "TL", "swaziland": "SZ", "cape verde": "CV", "macedonia": "MK", "turkiye": "TR",
    "vatican city": "VA", "holy see": "VA", "caribbean netherlands": "BQ", "bonaire": "BQ", "palestine": "PS",
    "svalbard": "SJ", "the gambia": "GM", "the bahamas": "BS", "russian federation": "RU", "laos": "LA",
    "micronesia": "FM", "st kitts and nevis": "KN", "st lucia": "LC", "st vincent and the grenadines": "VC",
    "sao tome and principe": "ST", "brunei darussalam": "BN", "syrian arab republic": "SY", "viet nam": "VN",
}


class LookupError_(Exception):
    """Raised when an external lookup fails in a way worth showing the user."""


def _fold(text):
    """Lowercase without accents: 'Montréal' → 'montreal', 'Zürich' → 'zurich'."""
    return "".join(c for c in unicodedata.normalize("NFKD", str(text or "")) if not unicodedata.combining(c)).lower()


_COUNTRY_INDEX = {re.sub(r"\s+", " ", _fold(v).replace("-", " ")): k for k, v in COUNTRIES.items()}


@lru_cache(maxsize=256)
def _open_meteo(name, count):
    res = requests.get(GEO_URL, params={"name": name, "count": count, "language": "en", "format": "json"}, timeout=10)
    res.raise_for_status()
    out = []
    for r in res.json().get("results") or []:
        code = (r.get("country_code") or "").upper()
        country = r.get("country") or COUNTRIES.get(code, code)
        out.append({
            "name": r.get("name"),
            "population": r.get("population") or 0,
            "region": [r.get("admin1"), r.get("admin2")],
            "country": country,
            "country_code": code,
            "lat": r["latitude"], "lon": r["longitude"],
            "elevation": r.get("elevation") or 0,
            "timezone": r.get("timezone"),
            "label": ", ".join(dict.fromkeys(p for p in [r.get("name"), r.get("admin1"), country] if p)),
            "source": "GeoNames",
        })
    return tuple(out)


@lru_cache(maxsize=256)
def _photon(text, count):
    res = requests.get(PHOTON_URL, params={"q": text, "limit": count, "lang": "en"},
                       headers={"User-Agent": USER_AGENT}, timeout=10)
    res.raise_for_status()
    out = []
    for f in res.json().get("features") or []:
        p = f.get("properties") or {}
        if not (p.get("osm_key") == "place" and p.get("osm_value") in _PLACE_VALUES
                or p.get("osm_key") == "boundary" and p.get("type") in ("city", "district")):
            continue
        lon, lat = f["geometry"]["coordinates"][:2]
        name = p.get("name")
        city = p.get("city") if p.get("city") and _fold(p.get("city")) != _fold(name) else None
        parts = [name, city, p.get("state"), p.get("country")]
        out.append({
            "name": name,
            "region": [p.get("state"), p.get("county"), city],
            "country": p.get("country", ""),
            "country_code": (p.get("countrycode") or "").upper(),
            "lat": lat, "lon": lon,
            "elevation": 0,      # the forecast supplies terrain height and
            "timezone": None,    # time zone for these
            "label": ", ".join(dict.fromkeys(x for x in parts if x)),
            "source": "OpenStreetMap",
        })
    return tuple(out)


def _country_code(text):
    """ISO code if `text` names a country ("Canada", "DR Congo", "the Netherlands"), else None."""
    t = re.sub(r"\s+", " ", _fold(text).replace(".", "").replace("-", " ").replace("’", "'")).strip()
    return _COUNTRY_INDEX.get(t) or _COUNTRY_ALIASES.get(t) or _COUNTRY_ALIASES.get(t.replace(" ", "-"))


def _matches(cand, qualifiers):
    """Does a candidate's region/country fit every qualifier ("ON", "Ontario", "Canada", "US")?"""
    words = set()
    texts = [*cand["region"], cand["country"]]
    for t in texts:
        words.update(re.findall(r"\w+", _fold(t)))
    folded = " ".join(_fold(t) for t in texts if t)
    for q in qualifiers:
        q = _fold(q).strip(" .")
        if not q:
            continue
        full = _REGION_ABBR.get(q) or _REGION_ABBR.get(q.replace(".", ""))
        code = _country_code(q)
        if code and not full:
            if code == cand["country_code"] or _fold(COUNTRIES.get(code, "")) in folded:
                continue
            return False
        if full:
            # A known code only means what it stands for (or a country code): "ON" is Ontario, not
            # any region that happens to contain the letters "on".
            names = full if isinstance(full, tuple) else (full,)
            if q.upper() == cand["country_code"] or any(n.upper() == cand["country_code"] or _fold(n) in folded for n in names):
                continue
            return False
        if q.upper() == cand["country_code"] or (len(q) > 3 and q in folded):
            continue
        if all(any(w.startswith(part) for w in words) for part in q.split()):
            continue
        return False
    return True


def _km(a, b):
    dlat, dlon = math.radians(b["lat"] - a["lat"]), math.radians(b["lon"] - a["lon"])
    h = math.sin(dlat / 2) ** 2 + math.cos(math.radians(a["lat"])) * math.cos(math.radians(b["lat"])) * math.sin(dlon / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


def _dedupe(cands):
    out = []
    for c in cands:
        n = _fold(c["name"])
        if any(_km(c, o) < 15 and (n == _fold(o["name"]) or n.startswith(_fold(o["name"])) or _fold(o["name"]).startswith(n))
               for o in out):
            continue
        out.append(c)
    return out


def search(query, count=8, fast=False):
    """
    Candidate locations for free text. "Name, Region, Country" filters by region; without commas,
    trailing words are tried as a region if the whole text isn't a place ("Gravenhurst Ontario").
    fast=True skips OpenStreetMap (~1.5 s slower) so as-you-type results appear quickly; the
    dashboard then asks again without it.
    """
    text = " ".join((query or "").split())
    if len(text) < 2:
        return []
    head, *rest = [p.strip() for p in text.split(",")]
    qualifiers = [p for p in rest if p]
    errors = []

    def safe(fn, *args):
        try:
            return list(fn(*args))
        except (requests.RequestException, ValueError, KeyError) as exc:
            errors.append(exc)
            return []

    with ThreadPoolExecutor(max_workers=2) as pool:
        om_head = pool.submit(safe, _open_meteo, head, 20)
        ph_full = pool.submit(safe, _photon, text, 12) if not fast else None
        om_head, ph_full = om_head.result(), (ph_full.result() if ph_full else [])
    # Places actually called what you typed first (GeoNames also matches alternate names: Portland
    # is "Bridgetown" in some lists), then the biggest among same-name towns.
    want = _fold(head)
    om_head.sort(key=lambda c: (0 if _fold(c["name"]) == want else 1 if _fold(c["name"]).startswith(want) else 2, -c["population"]))

    # "Gravenhurst Ontario": the whole head isn't a place name, so try trailing words as a region.
    if not qualifiers and not om_head and " " in head:
        words = head.split()
        for cut in range(len(words) - 1, 0, -1):
            found = safe(_open_meteo, " ".join(words[:cut]), 20)
            inferred = [" ".join(words[cut:])]
            if found and any(_matches(c, inferred) for c in found):
                om_head, qualifiers = found, inferred
                break

    if qualifiers:
        fitting = [c for c in om_head + ph_full if _matches(c, qualifiers)]
        # Nothing fits the region: show what OpenStreetMap made of the full text (its labels show
        # where each place is), never unrelated same-name towns from elsewhere.
        ranked = fitting or ph_full
    else:
        # The biggest same-name towns, then OpenStreetMap's (suburbs, merged towns like Scarborough ON).
        ranked = om_head[:4] + ph_full + om_head[4:]
    if not ranked and errors and len(errors) == (1 if fast else 2):
        raise LookupError_(f"Location search failed: {errors[0]}")
    return [{k: c[k] for k in ("name", "label", "country", "country_code", "lat", "lon", "elevation", "timezone", "source")}
            for c in _dedupe(ranked)[:count]]


def find(city, country=None):
    """Best match for a city, optionally restricted to a country by name or code ("US", "USA", "Canada")."""
    results = search(f"{city}, {country}" if country else city, count=10)
    return results[0] if results else None

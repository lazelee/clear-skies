"""
Build clearskies/data/openngc.json.gz from the OpenNGC database (NGC, IC, Messier and addendum).

    python scripts/build_catalog.py

OpenNGC by Mattia Verga, CC-BY-SA-4.0: https://github.com/mattiaverga/OpenNGC
"""
import csv
import gzip
import io
import json
import re
from pathlib import Path

import requests

BASE = "https://raw.githubusercontent.com/mattiaverga/OpenNGC/master/database_files/"
OUT = Path(__file__).resolve().parent.parent / "clearskies" / "data" / "openngc.json.gz"

TYPES = {
    "G": "Galaxy", "GPair": "Galaxy group", "GTrpl": "Galaxy group", "GGroup": "Galaxy group",
    "OCl": "Open cluster", "GCl": "Globular cluster", "*Ass": "Stellar association",
    "PN": "Planetary nebula", "HII": "Emission nebula", "EmN": "Emission nebula", "Neb": "Nebula",
    "RfN": "Reflection nebula", "Cl+N": "Cluster + nebula", "SNR": "Supernova remnant",
    "DrkN": "Dark nebula", "*": "Star", "**": "Double star", "Nova": "Nova", "Other": "Other",
}


def _short(name):
    """NGC0224 → NGC224, IC0001 → IC1, B033 → B33, C009 → C9 (Caldwell); others unchanged."""
    m = re.match(r"^(NGC|IC|B|C)0*(\d+)(.*)$", name)
    return f"{m.group(1)}{m.group(2)}{m.group(3)}" if m else name


def _deg(ra, dec):
    h, m, s = (float(x) for x in ra.split(":"))
    sign = -1 if dec.startswith("-") else 1
    d, dm, ds = (float(x) for x in dec.lstrip("+-").split(":"))
    return round((h + m / 60 + s / 3600) * 15, 5), round(sign * (d + dm / 60 + ds / 3600), 5)


def main():
    rows = []
    for name in ("NGC.csv", "addendum.csv"):
        text = requests.get(BASE + name, timeout=60).text
        rows += list(csv.DictReader(io.StringIO(text), delimiter=";"))

    out = []
    for r in rows:
        if r["Type"] in ("Dup", "NonEx") or not r["RA"] or not r["Dec"]:
            continue
        ra, dec = _deg(r["RA"], r["Dec"])
        short = _short(r["Name"])
        messier = f"M{int(r['M'])}" if r["M"].strip() else None
        common = [c.strip() for c in r["Common names"].split(",") if c.strip()]
        aliases = [short] + ([messier] if messier else [])
        for ident in r["Identifiers"].split(","):
            ident = ident.strip()
            m = re.match(r"^C 0*(\d+)$", ident)  # Caldwell
            if m:
                aliases.append(f"C{m.group(1)}")
            elif re.match(r"^(LBN|LDN|Sh2-|UGC|PGC|Mel|Cr|Abell)", ident):
                aliases.append(re.sub(r"\s+0*", " ", ident))
        mag = r["V-Mag"] or r["B-Mag"]
        out.append([
            messier or short,                       # id
            common[0] if common else "",            # common name
            TYPES.get(r["Type"], "Other"),          # type
            ra, dec,
            float(r["MajAx"]) if r["MajAx"] else None,  # major axis, arcmin
            float(mag) if mag else None,            # magnitude
            r["Const"],
            list(dict.fromkeys(aliases + common[1:])),
        ])
    OUT.parent.mkdir(exist_ok=True)
    with gzip.open(OUT, "wt", encoding="utf-8") as f:
        json.dump({"source": "OpenNGC (Mattia Verga), CC-BY-SA-4.0", "fields": ["id", "common", "type", "ra", "dec", "size", "mag", "const", "aliases"], "objects": out}, f, separators=(",", ":"))
    print(f"{len(out)} objects → {OUT} ({OUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()

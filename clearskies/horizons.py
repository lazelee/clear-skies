"""
Comets, asteroids and other moving bodies via NASA JPL Horizons (https://ssd.jpl.nasa.gov/horizons/).

Positions come back as topocentric astrometric RA/Dec (ICRF) for the observer's site, hourly,
and are interpolated onto the planning grid.
"""
import re
from datetime import datetime, timezone

import numpy as np
import requests

from . import db

API = "https://ssd.jpl.nasa.gov/api/horizons.api"
CACHE_SECONDS = 6 * 3600
MAJOR = {"pluto": "999"}  # dwarf planet Pluto is a "major body" in Horizons


class HorizonsError(Exception):
    def __init__(self, message, matches=None):
        super().__init__(message)
        self.matches = matches or []


def command_for(text):
    """Horizons COMMAND for what someone typed: comet designations, asteroid numbers or names."""
    t = text.strip()
    if re.match(r"^#\d+$", t):  # a Horizons record number picked from a list of matches
        return f"{t[1:]};"
    if t.lower() in MAJOR:
        return MAJOR[t.lower()]
    if re.match(r"^\d+[PI](/.*)?$", t, re.I) or re.match(r"^[PCDXIA]/\d{4}", t, re.I):
        # Periodic comets have one record per apparition: take the one closest to now.
        # "1P/Halley" → "1P"; "C/2023 A3" stays whole.
        des = t.split("/")[0] if re.match(r"^\d+[PI]/", t, re.I) else t
        return f"DES={des};CAP;NOFRAG;"
    return f"{t};"  # trailing ';' = small-body search (name, number or designation)


def _query(command, lat, lon, elev_m, start, stop, step="1h"):
    params = {
        "format": "json", "COMMAND": f"'{command}'", "OBJ_DATA": "NO", "MAKE_EPHEM": "YES",
        "EPHEM_TYPE": "OBSERVER", "CENTER": "'coord@399'", "COORD_TYPE": "GEODETIC",
        "SITE_COORD": f"'{lon:.5f},{lat:.5f},{elev_m / 1000:.3f}'",
        "START_TIME": f"'{start}'", "STOP_TIME": f"'{stop}'", "STEP_SIZE": f"'{step}'",
        "QUANTITIES": "'1'", "ANG_FORMAT": "DEG", "CSV_FORMAT": "YES", "TIME_TYPE": "UT",
    }
    try:
        res = requests.get(API, params=params, timeout=40)
        res.raise_for_status()
        out = res.json()
    except (requests.RequestException, ValueError) as exc:
        raise HorizonsError(f"JPL Horizons unavailable: {exc}") from exc
    if "error" in out:
        raise HorizonsError(out["error"])
    return out.get("result", "")


def _parse(result):
    if "$$SOE" not in result:
        matches = []
        if "Matching small-bodies" in result or "major-bodies" in result:
            table = result.split("-----")[-1] if "-----" in result else result
            for line in table.splitlines():
                m = re.match(r"^\s+(\d{1,9})\s{2,}(.*\S)\s*$", line)
                if m:
                    cols = re.split(r"\s{2,}", m.group(2))
                    # Periodic comets list one record per apparition; offer the designation once instead.
                    des = next((c for c in cols if re.match(r"^\d+[PI]$", c)), None)
                    label = " · ".join(c for c in cols if not re.match(r"^-?\d{1,4}$", c))
                    matches.append({"query": des or f"#{m.group(1)}", "label": label})
        if matches:
            unique = list({m["query"]: m for m in matches}.values())
            raise HorizonsError("Several objects match; pick one.", unique[:15])
        if "No matches found" in result or "Small-body Index Search" in result:
            raise HorizonsError("JPL Horizons has no comet or asteroid by that name.")
        raise HorizonsError(re.sub(r"\s+", " ", result.strip())[:200] or "Horizons found no match.")
    name = re.search(r"Target body name:\s*(.+?)(?:\s{2,}|\s*\{|$)", result, re.M)
    rows = result[result.index("$$SOE") + 5:result.index("$$EOE")].strip().splitlines()
    t, ra, dec = [], [], []
    for row in rows:
        cols = [c.strip() for c in row.split(",")]
        stamp = datetime.strptime(cols[0], "%Y-%b-%d %H:%M").replace(tzinfo=timezone.utc)
        t.append(stamp.timestamp())
        ra.append(float(cols[3]))
        dec.append(float(cols[4]))
    return (name.group(1).strip() if name else None), np.array(t), np.array(ra), np.array(dec)


def resolve(text):
    """Check Horizons knows `text`; returns (command, display name). Raises HorizonsError."""
    command = command_for(text)
    now = datetime.now(timezone.utc)
    day = now.strftime("%Y-%m-%d")
    name, *_ = _parse(_query(command, 0.0, 0.0, 0.0, f"{day} 00:00", f"{day} 01:00"))
    return command, name or text


def track(command, lat, lon, elev_m, start_unix, stop_unix):
    """Hourly (unix, ra, dec) for the planning window, cached for a few hours."""
    key = f"jpl:{command}:{round(lat, 2)},{round(lon, 2)}:{int(start_unix) // 86400}"
    cached = db.cache_get(key, CACHE_SECONDS)
    if cached:
        return cached["name"], np.array(cached["t"]), np.array(cached["ra"]), np.array(cached["dec"])
    fmt = lambda u: datetime.fromtimestamp(u, timezone.utc).strftime("%Y-%m-%d %H:%M")  # noqa: E731
    name, t, ra, dec = _parse(_query(command, lat, lon, elev_m, fmt(start_unix - 3600), fmt(stop_unix + 3600)))
    db.cache_put(key, {"name": name, "t": t.tolist(), "ra": ra.tolist(), "dec": dec.tolist()})
    return name, t, ra, dec


def on_grid(t_hourly, ra, dec, grid):
    """Interpolate an hourly track onto the grid (RA unwrapped so 359°→1° doesn't sweep backwards)."""
    ra_u = np.degrees(np.unwrap(np.radians(ra)))
    return np.interp(grid, t_hourly, ra_u) % 360, np.interp(grid, t_hourly, dec)

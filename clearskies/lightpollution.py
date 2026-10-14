"""
Zenith sky brightness anywhere from David Lorenz's light-pollution atlas (VIIRS satellite data).
https://djlorenz.github.io/astronomy/lp/

The atlas is a 1/40° world map (65°S–75°N) of 15 colour classes. Each class is a band of the
light-pollution index LPI = artificial / natural sky brightness: LPI = 1 at the 3b/4a boundary,
each zone number ×3 and each a/b half-step ×√3. Total brightness is then
22.0 − 2.5·log10(1 + LPI) mag/arcsec².

The map is downloaded once (2.8 MB) on first use and kept as a memory-mapped array in data/.
"""
import math
from pathlib import Path

import numpy as np
import requests

ATLAS_URL = "https://djlorenz.github.io/astronomy/lp2025/world2025_low3.png"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
ATLAS_NPY = DATA_DIR / "lp2025_world_40.npy"
RES = 40  # pixels per degree
NORTH, SOUTH = 75.0, -65.0

# Colour key, darkest first: zone 0, 1a, 1b, 2a, … 7b.
ZONES = ["0", "1a", "1b", "2a", "2b", "3a", "3b", "4a", "4b", "5a", "5b", "6a", "6b", "7a", "7b"]
COLORS = [
    (0, 0, 0), (34, 34, 34), (66, 66, 66), (20, 47, 114), (33, 84, 216), (15, 87, 20), (31, 161, 42),
    (110, 100, 30), (184, 166, 37), (191, 100, 30), (253, 150, 80), (251, 90, 73), (251, 153, 138),
    (160, 160, 160), (242, 242, 242),
]
NATURAL = 22.0


def _lpi(cls):
    """Geometric middle of a class's LPI band. Class 7 (4a) starts at LPI 1; each class is ×√3."""
    if cls == 0:
        return 0.01  # zone 0: essentially pristine
    lower = 3 ** ((cls - 7) / 2)
    return lower * 3 ** 0.25


def _build():
    from io import BytesIO

    from PIL import Image

    DATA_DIR.mkdir(exist_ok=True)
    res = requests.get(ATLAS_URL, timeout=60)
    res.raise_for_status()
    img = Image.open(BytesIO(res.content))
    if img.mode != "P":
        raise ValueError("unexpected atlas format")
    pal = img.getpalette()
    lut = np.full(256, 255, np.uint8)
    for idx in range(len(pal) // 3):
        rgb = tuple(pal[3 * idx:3 * idx + 3])
        if rgb in COLORS:
            lut[idx] = COLORS.index(rgb)
    arr = lut[np.asarray(img, dtype=np.uint8)]
    tmp = ATLAS_NPY.with_suffix(".tmp.npy")
    np.save(tmp, arr)
    tmp.replace(ATLAS_NPY)


_atlas = None


def _load():
    global _atlas
    if _atlas is None:
        if not ATLAS_NPY.exists():
            _build()
        _atlas = np.load(ATLAS_NPY, mmap_mode="r")
    return _atlas


def lookup(lat, lon):
    """
    {"sqm", "bortle", "zone", "lpi", "source"} for a location, or a natural-sky estimate outside
    the atlas (polar regions, which have no significant light pollution) or if it can't be loaded.
    """
    from .skyquality import bortle_from_sqm

    if not SOUTH <= lat < NORTH:
        return {"sqm": NATURAL, "bortle": 1, "zone": None, "lpi": 0.0, "source": "outside atlas (assumed natural sky)"}
    try:
        atlas = _load()
    except Exception:
        return None
    row = min(int((NORTH - lat) * RES), atlas.shape[0] - 1)
    col = int(((lon + 180) % 360) * RES) % atlas.shape[1]
    cls = int(atlas[row, col])
    if cls == 255:
        return None
    lpi = _lpi(cls)
    sqm = round(NATURAL - 2.5 * math.log10(1 + lpi), 2)
    return {"sqm": sqm, "bortle": bortle_from_sqm(sqm), "zone": ZONES[cls], "lpi": round(lpi, 2), "source": "VIIRS 2025 atlas"}

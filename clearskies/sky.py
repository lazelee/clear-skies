"""Sun, moon and target positions on a 10-minute grid, computed offline with astropy."""
import warnings
from functools import lru_cache

import numpy as np
from astropy.utils import iers

# Never block on (or fail for lack of) an IERS table download; the built-in tables are
# accurate to well under a second, far below what planning a night needs.
iers.conf.auto_download = False
iers.conf.auto_max_age = None

import astropy.units as u  # noqa: E402
from astropy.coordinates import AltAz, EarthLocation, GeocentricTrueEcliptic, SkyCoord, get_body  # noqa: E402
from astropy.time import Time  # noqa: E402

warnings.filterwarnings("ignore", module="astropy")
warnings.filterwarnings("ignore", message=".*ERFA.*")

STEP_SECONDS = 600


def grid(start_unix, count):
    return start_unix + np.arange(count) * STEP_SECONDS


@lru_cache(maxsize=16)
def sun_moon(lat, lon, elevation, start_unix, count):
    """Altitudes (deg) of sun and moon, moon illuminated fraction, and signed elongation."""
    t = Time(grid(start_unix, count), format="unix")
    loc = EarthLocation(lat=lat * u.deg, lon=lon * u.deg, height=elevation * u.m)
    frame = AltAz(obstime=t, location=loc)
    sun = get_body("sun", t, loc)
    moon = get_body("moon", t, loc)
    sun_alt = sun.transform_to(frame).alt.deg
    moon_alt = moon.transform_to(frame).alt.deg
    # Signed sun-moon ecliptic longitude difference: 0 = new, 180 = full, <180 waxing.
    ecl = GeocentricTrueEcliptic(equinox=t)
    elong = (moon.transform_to(ecl).lon.deg - sun.transform_to(ecl).lon.deg) % 360
    # Illuminated fraction k = (1 + cos i) / 2 from the true phase angle i (Sun–Moon–Earth),
    # in 3D: the longitude difference alone ignores the moon's ±5° ecliptic latitude.
    s = get_body("sun", t).cartesian.xyz.to_value(u.km)
    m = get_body("moon", t).cartesian.xyz.to_value(u.km)
    to_sun, to_earth = s - m, -m
    cos_i = (to_sun * to_earth).sum(0) / (np.linalg.norm(to_sun, axis=0) * np.linalg.norm(to_earth, axis=0))
    illum = (1 + cos_i) / 2
    return sun_alt, moon_alt, illum, elong


@lru_cache(maxsize=64)
def target_alt(lat, lon, elevation, ra, dec, start_unix, count):
    t = Time(grid(start_unix, count), format="unix")
    loc = EarthLocation(lat=lat * u.deg, lon=lon * u.deg, height=elevation * u.m)
    coord = SkyCoord(ra=ra * u.deg, dec=dec * u.deg, frame="icrs")
    return coord.transform_to(AltAz(obstime=t, location=loc)).alt.deg


def phase_name(elong):
    e = elong % 360
    if e < 10 or e > 350:
        return "New Moon"
    if e < 80:
        return "Waxing Crescent"
    if e < 100:
        return "First Quarter"
    if e < 170:
        return "Waxing Gibbous"
    if e < 190:
        return "Full Moon"
    if e < 260:
        return "Waning Gibbous"
    if e < 280:
        return "Last Quarter"
    return "Waning Crescent"

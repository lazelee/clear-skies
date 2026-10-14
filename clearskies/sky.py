"""Sun, moon and target positions on a 10-minute grid, computed offline with astropy."""
import warnings
from functools import lru_cache

import numpy as np
from astropy.utils import iers

# Never block on (or fail for lack of) an IERS table download; the built-in tables are
# accurate to well under a second, far below what planning a night needs.
iers.conf.auto_download = False
iers.conf.auto_max_age = None
# Past the bundled table's end, UT1−UTC is taken as 0 (|UT1−UTC| < 0.9 s by definition).
iers.conf.iers_degraded_accuracy = "ignore"

import astropy.units as u  # noqa: E402
from astropy.coordinates import AltAz, EarthLocation, GeocentricTrueEcliptic, SkyCoord, get_body  # noqa: E402
from astropy.time import Time  # noqa: E402

warnings.filterwarnings("ignore", module="astropy")
warnings.filterwarnings("ignore", message=".*ERFA.*")

STEP_SECONDS = 600


def grid(start_unix, count):
    return start_unix + np.arange(count) * STEP_SECONDS


def _setup(lat, lon, elevation, start_unix, count):
    t = Time(grid(start_unix, count), format="unix")
    loc = EarthLocation(lat=lat * u.deg, lon=lon * u.deg, height=elevation * u.m)
    return t, loc, AltAz(obstime=t, location=loc)


@lru_cache(maxsize=16)
def sun_moon(lat, lon, elevation, start_unix, count):
    """
    Sun and moon on the grid: altitudes, moon azimuth, illuminated fraction, phase angle
    (0 = full, 180 = new) and signed elongation (0 = new, 180 = full, < 180 waxing).
    Positions are topocentric, so the moon's ~1° parallax is included.
    """
    t, loc, frame = _setup(lat, lon, elevation, start_unix, count)
    sun = get_body("sun", t, loc)
    moon = get_body("moon", t, loc)
    sun_alt = sun.transform_to(frame).alt.deg
    moon_aa = moon.transform_to(frame)
    ecl = GeocentricTrueEcliptic(equinox=t)
    elong = (moon.transform_to(ecl).lon.deg - sun.transform_to(ecl).lon.deg) % 360
    # Phase angle i (Sun–Moon–Earth) in 3D; the longitude difference alone ignores the
    # moon's ±5° ecliptic latitude. Illuminated fraction k = (1 + cos i) / 2.
    s = get_body("sun", t).cartesian.xyz.to_value(u.km)
    m = get_body("moon", t).cartesian.xyz.to_value(u.km)
    to_sun, to_earth = s - m, -m
    cos_i = (to_sun * to_earth).sum(0) / (np.linalg.norm(to_sun, axis=0) * np.linalg.norm(to_earth, axis=0))
    return {
        "sun_alt": sun_alt,
        "moon_alt": moon_aa.alt.deg,
        "moon_az": moon_aa.az.deg,
        "illum": (1 + cos_i) / 2,
        "phase_angle": np.degrees(np.arccos(np.clip(cos_i, -1, 1))),
        "elong": elong,
    }


@lru_cache(maxsize=64)
def fixed_altaz(lat, lon, elevation, ra, dec, start_unix, count):
    """Alt/az of a fixed ICRS (J2000) position; precession, nutation and aberration are applied."""
    t, _, frame = _setup(lat, lon, elevation, start_unix, count)
    aa = SkyCoord(ra=ra * u.deg, dec=dec * u.deg, frame="icrs").transform_to(frame)
    return aa.alt.deg, aa.az.deg


@lru_cache(maxsize=32)
def body_altaz(lat, lon, elevation, body, start_unix, count):
    """Alt/az of a solar-system body astropy knows (the moon and the planets Mercury–Neptune)."""
    t, loc, frame = _setup(lat, lon, elevation, start_unix, count)
    aa = get_body(body, t, loc).transform_to(frame)
    return aa.alt.deg, aa.az.deg


def track_altaz(lat, lon, elevation, ra, dec, start_unix, count):
    """Alt/az of a moving object from per-slot topocentric astrometric RA/Dec (e.g. from JPL Horizons)."""
    t, _, frame = _setup(lat, lon, elevation, start_unix, count)
    aa = SkyCoord(ra=np.asarray(ra) * u.deg, dec=np.asarray(dec) * u.deg, frame="icrs").transform_to(frame)
    return aa.alt.deg, aa.az.deg


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

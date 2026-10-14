"""Sky-quality physics and ephemeris checked against published values and almanacs."""
from datetime import datetime, timezone

import numpy as np
import pytest

from clearskies import sky, skyquality as q


def test_airmass_matches_kasten_young():
    x = q.airmass([90, 60, 30, 10])
    assert x[0] == pytest.approx(1.0, abs=1e-3)
    assert x[1] == pytest.approx(1.1547, abs=2e-3)  # sec(30°)
    assert x[2] == pytest.approx(1.994, abs=5e-3)
    assert x[3] == pytest.approx(5.59, abs=0.05)
    assert np.isnan(q.airmass(-1))


def test_extinction_falls_with_altitude_and_rises_with_haze():
    sea, peak = q.extinction_coefficient(0), q.extinction_coefficient(4200, 0.02)
    assert 0.2 < sea < 0.35  # typical low-site V extinction
    assert 0.08 < peak < 0.15  # Mauna Kea class
    assert q.extinction_coefficient(0, 0.5) > sea + 0.4  # smoke/dust


def test_full_moon_sky_brightness_krisciunas_schaefer():
    # A full moon high in the sky brightens a dark site (21.9) to ~18–19 mag/arcsec², as measured.
    d = q.moon_brightening_mag(21.9, 60, 0, 60, 60, 0.172)
    assert 18.0 < 21.9 - d < 19.0


def test_moon_matters_less_under_bright_skies():
    dark = q.moon_brightening_mag(21.9, 45, 60, 60, 60, 0.2)
    city = q.moon_brightening_mag(18.0, 45, 60, 60, 60, 0.2)
    assert dark > 4 * city


def test_moon_below_horizon_adds_nothing():
    assert q.moon_brightening_mag(21.5, -5, 0, 30, 60, 0.2) == pytest.approx(0)


def test_deep_sky_weight_reference_and_bounds():
    # Target overhead, moon down, clear sky: exactly the reference condition.
    assert q.deep_sky_weight(21.0, 90, -10, 0, 90, 0.2, 0) == pytest.approx(1.0, abs=1e-6)
    w = q.deep_sky_weight(21.0, np.array([80, 50, 30, 15]), -10, 0, 90, 0.2, 0)
    assert np.all(np.diff(w) < 0) and np.all(w > 0)  # lower altitude is always worth less
    assert q.deep_sky_weight(21.0, 60, -10, 0, 90, 0.2, 50) == pytest.approx(0.5 * q.deep_sky_weight(21.0, 60, -10, 0, 90, 0.2, 0))


def test_planetary_weight_prefers_calm_upper_air():
    calm = q.planetary_weight(60, 0.2, 0, 30)
    jet = q.planetary_weight(60, 0.2, 0, 200)
    assert calm > 2 * jet


def test_bortle_from_sqm_table():
    assert q.bortle_from_sqm(21.95) == 2
    assert q.bortle_from_sqm(21.3) == 4
    assert q.bortle_from_sqm(20.0) == 5
    assert q.bortle_from_sqm(17.0) == 9


def _at(iso):
    return int(datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp())


def test_moon_illumination_at_published_phases():
    # USNO: full moon 2026-10-26 04:12 UT, first quarter 2026-10-18 16:12 UT.
    full = sky.sun_moon(44.92, -79.37, 0, _at("2026-10-26T04:10"), 1)
    quarter = sky.sun_moon(44.92, -79.37, 0, _at("2026-10-18T16:10"), 1)
    assert full["illum"][0] > 0.995
    assert quarter["illum"][0] == pytest.approx(0.5, abs=0.01)


def test_moonset_matches_usno_almanac():
    # USNO: moonset at Gravenhurst (44.92 N, 79.37 W) on 2026-10-16 at 22:00 EDT = 02:00 UT on the 17th.
    start = _at("2026-10-17T01:00")
    sm = sky.sun_moon(44.92, -79.37, 0, start, 13)
    alt, t = sm["moon_alt"], sky.grid(start, 13)
    i = int(np.flatnonzero((alt[:-1] >= -0.833) & (alt[1:] < -0.833))[0])
    setting = t[i] + (alt[i] + 0.833) / (alt[i] - alt[i + 1]) * sky.STEP_SECONDS
    assert abs(setting - _at("2026-10-17T02:00")) < 180  # within 3 minutes


def test_angular_separation():
    assert q.angular_separation(90, 0, 0, 123) == pytest.approx(90)
    assert q.angular_separation(30, 10, 30, 10) == pytest.approx(0, abs=1e-6)

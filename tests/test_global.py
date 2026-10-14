"""
The planner across the world: every latitude band, the date line, odd time zones, DST changes,
polar night and midnight sun, and every kind of target. Weather is synthetic (always clear) so
these run offline and test the astronomy and bookkeeping, not the forecast.
"""
import re
import time

import numpy as np
import pytest

from clearskies import planner, targets, weather

SITES = [
    ("Gravenhurst", 44.92, -79.37, 260, "America/Toronto"),
    ("Sydney", -33.87, 151.21, 30, "Australia/Sydney"),
    ("Cerro Paranal", -24.63, -70.40, 2635, "America/Santiago"),
    ("Quito (equator)", -0.18, -78.47, 2850, "America/Guayaquil"),
    ("Tromsø", 69.65, 18.96, 10, "Europe/Oslo"),
    ("Longyearbyen", 78.22, 15.65, 10, "Arctic/Longyearbyen"),
    ("McMurdo", -77.85, 166.67, 10, "Antarctica/McMurdo"),
    ("South Pole", -90.0, 0.0, 2835, "Antarctica/McMurdo"),
    ("North Pole", 90.0, 0.0, 0, "UTC"),
    ("Kiritimati (UTC+14)", 1.87, -157.36, 5, "Pacific/Kiritimati"),
    ("Chatham Is. (+12:45)", -43.95, -176.55, 30, "Pacific/Chatham"),
    ("Kathmandu (+5:45)", 27.70, 85.32, 1400, "Asia/Kathmandu"),
    ("St. John's (−3:30)", 47.56, -52.71, 60, "America/St_Johns"),
    ("Berlin (DST change)", 52.52, 13.40, 34, "Europe/Berlin"),
    ("Mauna Kea", 19.82, -155.47, 4200, "Pacific/Honolulu"),
    ("Date line west", -17.7, 179.99, 0, "Pacific/Fiji"),
    ("Date line east", -17.7, -179.99, 0, "Pacific/Fiji"),
]


@pytest.fixture(autouse=True)
def clear_weather(monkeypatch):
    """Every hour clear, dry and calm for the whole forecast; no ensemble or aerosol data."""
    def fake_fetch(lat, lon):
        t0 = int(time.time()) // 3600 * 3600 - 2 * 86400
        times = [t0 + 3600 * i for i in range(20 * 24)]
        values = {"cloud_cover": 0, "cloud_cover_low": 0, "cloud_cover_mid": 0, "cloud_cover_high": 0,
                  "precipitation_probability": 0, "temperature_2m": 10, "dew_point_2m": 0, "relative_humidity_2m": 50,
                  "wind_speed_10m": 5, "wind_gusts_10m": 10, "visibility": 50000, "wind_speed_250hPa": 40}
        return {"timezone": "UTC", "hourly": {"time": times, **{k: [v] * len(times) for k, v in values.items()}}}

    monkeypatch.setattr(weather, "fetch", fake_fetch)
    monkeypatch.setattr(weather, "fetch_ensemble", lambda lat, lon: None)
    monkeypatch.setattr(weather, "fetch_aerosol", lambda lat, lon: None)
    monkeypatch.setattr(planner, "_site_sky", lambda lat, lon: {"sqm": 21.5, "bortle": 3, "source": "test", "atlas": None})


def _plan(site, target="none"):
    _, lat, lon, elev, tz = site
    return planner.plan({"label": site[0], "lat": lat, "lon": lon, "elevation": elev, "timezone": tz}, target, {})


HHMM = re.compile(r"^\d\d:\d\d$")


@pytest.mark.parametrize("site", SITES, ids=[s[0] for s in SITES])
def test_every_site_produces_a_consistent_plan(site):
    p = _plan(site)
    nights = p["nights"]
    assert 14 <= len(nights) <= 15
    dates = [n["date"] for n in nights]
    assert dates == sorted(set(dates)), "nights must be unique and in order"
    for n in nights:
        assert 0 <= n["score"] <= 100
        assert 0 <= n["effective_hours"] <= n["usable_hours"] + 1e-9
        assert n["verdict"] in ("Excellent", "Good", "Marginal", "Poor")
        if n["darkness"] != "none":
            assert HHMM.match(n["dark_start"]) and HHMM.match(n["dark_end"])
        for w in n["windows"]:
            a, b = w.split("–")
            assert HHMM.match(a) and HHMM.match(b)
        tl = n["timeline"]
        assert len({len(tl[k]) for k in ("t", "labels", "sun", "moon", "cloud", "quality", "shoot")}) == 1
        assert n["reasons"], "every night explains itself"


def test_clear_dark_site_scores_well_somewhere():
    p = _plan(SITES[2])  # Paranal, always clear in this test
    assert max(n["score"] for n in p["nights"]) >= 75


def test_midnight_sun_has_no_darkness():
    p = _plan(next(s for s in SITES if s[0] == "South Pole"))
    assert all(n["darkness"] == "none" and n["score"] == 0 for n in p["nights"])


def test_north_pole_polar_night_is_dark_all_day():
    p = _plan(next(s for s in SITES if s[0] == "North Pole"))
    assert any(n["polar"] == "polar night" for n in p["nights"])
    dark = [n for n in p["nights"] if n["polar"] == "polar night" and n["darkness"] == "astronomical"]
    assert all(n["dark_hours"] > 20 for n in dark)


def test_dst_night_is_25_hours_long():
    p = _plan(next(s for s in SITES if s[0].startswith("Berlin")))
    lengths = {n["date"]: len(n["timeline"]["t"]) for n in p["nights"]}
    if "2026-10-24" in lengths:  # clocks go back early on Oct 25: that night gains an hour of darkness
        dark = {n["date"]: n["dark_hours"] for n in p["nights"]}
        assert dark["2026-10-24"] > dark.get("2026-10-23", 0) - 0.1


def test_southern_targets_rise_in_the_south_only():
    north = _plan(SITES[0], "LMC")
    south = _plan(SITES[1], "LMC")
    assert all(n["target"]["peak_alt"] < 0 for n in north["nights"])
    assert any("never rises" in r for n in north["nights"] for r in n["reasons"])
    assert max(n["target"]["peak_alt"] for n in south["nights"]) > 30


@pytest.mark.parametrize("target_id", ["M31", "NGC891", "IC1396", "B33", "C9", "body:jupiter", "body:moon", "body:saturn",
                                       "coord:83.82,-5.39;Orion|type=Emission nebula"])
def test_offline_target_kinds(target_id):
    p = _plan(SITES[0], target_id)
    assert p["target"]["id"] == target_id
    assert any(n["target"] and n["target"]["peak_alt"] > 0 for n in p["nights"])


def test_planets_use_civil_twilight_and_seeing_weight():
    p = _plan(SITES[0], "body:jupiter")
    assert p["target"]["mode"] == "planetary"
    assert all(n["darkness"] in ("civil", "none") for n in p["nights"])
    assert p["metric"]["label"] == "seeing-weighted hours"


def test_catalog_lookup_by_any_alias():
    for alias in ["M31", "NGC224", "ngc 224", "Andromeda Galaxy", "UGC 454", "C20", "NGC 7000"]:
        t = targets.get(alias)
        assert t["ra"] is not None, alias
    assert targets.get("C20")["id"] == "NGC7000"


def test_unknown_target_is_an_error_not_a_silent_default():
    with pytest.raises(ValueError):
        targets.get("NOT_A_REAL_OBJECT_123")


def test_moon_limit_setting_controls_moonlit_slots():
    strict = _plan(SITES[0], "M31")
    loose = planner.plan({"label": "x", "lat": 44.92, "lon": -79.37, "timezone": "America/Toronto"}, "M31", {"moon_limit": 5})
    assert sum(n["usable_minutes"] for n in loose["nights"]) >= sum(n["usable_minutes"] for n in strict["nights"])


def test_short_gaps_do_not_count(monkeypatch):
    # Alternate 20 minutes clear / 40 minutes cloudy: every gap is under 30 min, so nothing counts.
    def gappy(lat, lon):
        t0 = int(time.time()) // 3600 * 3600 - 2 * 86400
        times = [t0 + 600 * i for i in range(20 * 24 * 6)]
        cloud = [0 if (i % 6) < 2 else 100 for i in range(len(times))]
        base = {k: [0] * len(times) for k in weather.HOURLY}
        base["cloud_cover"] = cloud
        base["temperature_2m"] = [10] * len(times)
        base["wind_speed_250hPa"] = [40] * len(times)
        return {"timezone": "UTC", "hourly": {"time": times, **base}}

    monkeypatch.setattr(weather, "fetch", gappy)
    p = _plan(SITES[2])
    assert all(n["usable_minutes"] == 0 for n in p["nights"])


def test_settings_are_clamped():
    s = planner.clean_settings({"max_cloud": 500, "min_alt": -10, "moon_limit": "nan", "max_precip": "x"})
    assert s == {"max_cloud": 100, "max_precip": 15, "min_alt": 0, "moon_limit": 1.5}
    assert np.isfinite(planner.clean_settings({"moon_limit": "inf"})["moon_limit"])

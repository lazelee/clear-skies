"""Turns weather + ephemeris into a ranked plan of shootable nights."""
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np

from . import db, sky, targets, weather

DEFAULTS = {"max_cloud": 25, "max_precip": 15, "min_alt": 30, "moon_tol": 25}
LIMITS = {"max_cloud": (0, 100), "max_precip": (0, 100), "min_alt": (0, 80), "moon_tol": (0, 100)}
ASTRO_DARK = -18.0
NAUTICAL_DARK = -12.0
SUNSET_ALT = -0.833
# Topocentric altitude of the moon's centre when its upper limb is on the horizon:
# 34' refraction + ~15.5' semi-diameter (parallax is already in astropy's topocentric position).
MOON_HORIZON = -0.833
STEP_MIN = sky.STEP_SECONDS // 60
NIGHTS = 15


def clean_settings(raw):
    out = {}
    for key, default in DEFAULTS.items():
        try:
            value = float(raw.get(key, default))
        except (TypeError, ValueError):
            value = default
        lo, hi = LIMITS[key]
        out[key] = int(min(hi, max(lo, value)))
    return out


def _hhmm(unix, tz):
    return datetime.fromtimestamp(unix, tz).strftime("%H:%M")


def _crossings(alt, level, t):
    """Unix times where `alt` rises through / sets below `level`, interpolated between grid points."""
    above = alt >= level

    def at(i):
        a0, a1 = alt[i - 1], alt[i]
        frac = (level - a0) / (a1 - a0) if a1 != a0 else 0
        return int(t[i - 1] + frac * (t[i] - t[i - 1]))

    rises = [at(i) for i in range(1, len(alt)) if above[i] and not above[i - 1]]
    sets = [at(i) for i in range(1, len(alt)) if not above[i] and above[i - 1]]
    return rises, sets


def _blocks(mask):
    """Contiguous runs of True as (start, end_exclusive)."""
    runs, start = [], None
    for i, ok in enumerate(mask):
        if ok and start is None:
            start = i
        elif not ok and start is not None:
            runs.append((start, i))
            start = None
    if start is not None:
        runs.append((start, len(mask)))
    return runs


def _series(raw):
    return np.array([np.nan if v is None else v for v in raw or []], dtype=float)


def _interp_instant(times, arr, x):
    """Linear interpolation of values valid at `times`; NaN outside the series or next to a gap."""
    out = np.full(len(x), np.nan)
    if len(arr) < 2:
        return out
    i = np.searchsorted(times, x, side="right") - 1
    inside = (i >= 0) & (i < len(arr) - 1)
    i = np.clip(i, 0, len(arr) - 2)
    frac = (x - times[i]) / (times[i + 1] - times[i])
    out[inside] = (arr[i] * (1 - frac) + arr[i + 1] * frac)[inside]
    out[x == times[-1]] = arr[-1]
    return out


def _weather_on_grid(wx, t):
    """
    Put hourly weather on the 10-minute grid, respecting what each timestamp means
    (Open-Meteo): most variables are instantaneous at the hour, so interpolate between
    hours; precipitation probability covers the *preceding* hour, so a slot takes the
    value stamped at the end of the hour it falls in.
    """
    hourly = wx["hourly"]
    times = np.asarray(hourly["time"], dtype=float)
    mid = t + sky.STEP_SECONDS / 2
    out = {}
    for key in weather.HOURLY:
        arr = _series(hourly.get(key))
        if key in weather.PRECEDING_HOUR:
            vals = np.full(len(t), np.nan)
            if len(arr):
                j = np.clip(np.searchsorted(times, mid, side="left"), 0, len(arr) - 1)
                ok = (times[j] >= mid) & (times[j] - mid < 3600)
                vals[ok] = arr[j][ok]
        else:
            vals = _interp_instant(times, arr, mid)
        out[key] = vals
    return out


def _verdict(score):
    if score >= 75:
        return "Excellent"
    if score >= 50:
        return "Good"
    if score >= 25:
        return "Marginal"
    return "Poor"


def _tip(target, bortle):
    if not bortle or target["id"] == "none":
        return None
    kind = target["type"]
    if bortle >= 6 and kind in ("Emission nebula", "Supernova remnant", "Planetary nebula"):
        return f"Bortle {bortle} sky: a dual-band (Hα/OIII) filter will cut most of the light pollution."
    if bortle >= 7 and kind == "Galaxy":
        return f"Bortle {bortle} sky: galaxies need long integration here, or a trip to a darker site."
    if bortle >= 7 and kind == "Milky Way":
        return f"Bortle {bortle} sky: the Milky Way core will be washed out; find a darker site."
    return None


def plan(location, target_id="none", settings=None, now=None):
    """
    location: {label, lat, lon, elevation?, timezone?}
    Returns every night in the forecast with a score, windows and a chart timeline.
    """
    s = clean_settings(settings or {})
    target = targets.get(target_id)
    lat, lon = float(location["lat"]), float(location["lon"])
    elevation = float(location.get("elevation") or 0)

    wx = weather.fetch(lat, lon)
    tz = ZoneInfo(location.get("timezone") or wx.get("timezone") or "UTC")
    now = now or time.time()
    local_now = datetime.fromtimestamp(now, tz)

    # Nights run local noon → next noon. Before noon, last night is still "tonight".
    first = local_now.date() - (timedelta(days=1) if local_now.hour < 12 else timedelta(0))
    noons = [int(datetime.combine(first + timedelta(days=i), datetime.min.time(), tz).replace(hour=12).timestamp())
             for i in range(NIGHTS + 1)]
    start = noons[0] - noons[0] % sky.STEP_SECONDS
    count = (noons[-1] - start) // sky.STEP_SECONDS
    t = sky.grid(start, count)

    sun_alt, moon_alt, illum, elong = sky.sun_moon(round(lat, 4), round(lon, 4), round(elevation), start, count)
    tgt_alt = None
    if target["ra"] is not None:
        tgt_alt = sky.target_alt(round(lat, 4), round(lon, 4), round(elevation), target["ra"], target["dec"], start, count)
    wxg = _weather_on_grid(wx, t)

    bortle = db.bortle_for(lat, lon)
    nights = []
    for i in range(NIGHTS):
        a = int(np.searchsorted(t, noons[i]))
        b = int(np.searchsorted(t, noons[i + 1]))
        nights.append(_night(first + timedelta(days=i), t[a:b], sun_alt[a:b], moon_alt[a:b], illum[a:b], elong[a:b],
                             None if tgt_alt is None else tgt_alt[a:b], {k: v[a:b] for k, v in wxg.items()},
                             s, target, tz, now))

    # Before noon the list starts with last night; drop it once its darkness is over.
    if nights and nights[0]["reasons"] == ["This night is already over."]:
        nights = nights[1:]

    ranked = [n for n in nights if n["usable_minutes"] > 0]
    best = max(ranked, key=lambda n: (n["score"], n["usable_minutes"]), default=None)
    return {
        "location": {**location, "lat": lat, "lon": lon, "elevation": elevation, "timezone": str(tz.key), "bortle": bortle},
        "target": target,
        "settings": s,
        "generated_at": int(now),
        "weather_fetched_at": wx.get("fetched_at"),
        "nights": nights,
        "best": best and {k: best[k] for k in ("date", "label", "best_window", "score", "verdict", "usable_hours")},
        "good_nights": sum(1 for n in nights if n["score"] >= 50),
        "tip": _tip(target, bortle),
    }


def _night(day, t, sun, moon, illum, elong, tgt, wx, s, target, tz, now):
    label = day.strftime("%a %b %-d")
    night = {"date": day.isoformat(), "label": label}

    sunsets = _crossings(sun, SUNSET_ALT, t)[1]
    sunrises = _crossings(sun, SUNSET_ALT, t)[0]
    night["sunset"] = _hhmm(sunsets[0], tz) if sunsets else None
    night["sunrise"] = _hhmm(sunrises[-1], tz) if sunrises else None

    dark_level = ASTRO_DARK
    dark = sun <= ASTRO_DARK
    darkness = "astronomical"
    if not dark.any():
        dark_level, dark, darkness = NAUTICAL_DARK, sun <= NAUTICAL_DARK, "nautical"
        if not dark.any():
            darkness = "none"
    night["darkness"] = darkness
    dark_idx = np.flatnonzero(dark)
    dusk, dawn = _crossings(sun, dark_level, t)[1], _crossings(sun, dark_level, t)[0]
    night["dark_start"] = _hhmm(dusk[0], tz) if dusk and len(dark_idx) else None
    night["dark_end"] = _hhmm(dawn[-1], tz) if dawn and len(dark_idx) else None
    night["dark_hours"] = round(len(dark_idx) * STEP_MIN / 60, 1)

    # Moon: phase at the middle of the night, rise/set during it.
    mid = dark_idx[len(dark_idx) // 2] if len(dark_idx) else len(t) // 2
    # Only moon events between sunset and sunrise matter for the night.
    night_span = sun < 0
    m_rises, m_sets = (
        [x for x in ev if night_span[min(int(np.searchsorted(t, x)), len(t) - 1)]] for ev in _crossings(moon, MOON_HORIZON, t)
    )
    moon_up_dark = float((moon[dark] > MOON_HORIZON).mean()) if dark.any() else 0.0
    night["moon"] = {
        "phase": sky.phase_name(elong[mid]),
        "illumination": int(round(illum[mid] * 100)),
        "elongation": round(float(elong[mid]), 1),
        "rise": _hhmm(m_rises[0], tz) if m_rises else None,
        "set": _hhmm(m_sets[0], tz) if m_sets else None,
        "up_fraction": round(moon_up_dark, 2),
    }

    cloud = wx["cloud_cover"]
    precip = wx["precipitation_probability"]
    have_wx = ~np.isnan(cloud)
    future = t + sky.STEP_SECONDS > now
    cloud_ok = have_wx & (np.nan_to_num(cloud, nan=101) <= s["max_cloud"])
    # A missing precipitation value alone (common at the far end of the forecast) doesn't veto a slot.
    precip_ok = np.isnan(precip) | (precip <= s["max_precip"])
    moon_ok = (moon <= MOON_HORIZON) | (illum * 100 <= s["moon_tol"])
    tgt_ok = np.ones(len(t), bool) if tgt is None else tgt >= s["min_alt"]
    shoot = dark & future & cloud_ok & precip_ok & moon_ok & tgt_ok

    runs = _blocks(shoot)
    longest = max(runs, key=lambda r: r[1] - r[0], default=None)
    usable = int(shoot.sum()) * STEP_MIN
    night["usable_minutes"] = usable
    night["usable_hours"] = round(usable / 60, 1)
    # Windows are built from 10-minute slots; clip their edges to the exact dusk/dawn.
    lo_clip = dusk[0] if dusk else -np.inf
    hi_clip = dawn[-1] if dawn else np.inf

    def window(x, y):
        return f"{_hhmm(max(t[x], lo_clip), tz)}–{_hhmm(min(t[y - 1] + sky.STEP_SECONDS, hi_clip), tz)}"

    night["windows"] = [window(x, y) for x, y in runs]
    night["best_window"] = window(*longest) if longest else None
    longest_h = (longest[1] - longest[0]) * STEP_MIN / 60 if longest else 0
    night["longest_hours"] = round(longest_h, 1)

    ref = shoot if shoot.any() else (dark & have_wx)
    def avg(arr):
        vals = arr[ref & ~np.isnan(arr)]
        return int(round(float(vals.mean()))) if len(vals) else None

    night["avg_cloud"] = avg(cloud)
    night["clouds"] = {"low": avg(wx["cloud_cover_low"]), "mid": avg(wx["cloud_cover_mid"]), "high": avg(wx["cloud_cover_high"])}
    night["avg_precip"] = avg(precip)
    night["avg_wind"] = avg(wx["wind_speed_10m"])
    spread = wx["temperature_2m"] - wx["dew_point_2m"]
    dark_spread = spread[dark & ~np.isnan(spread)]
    night["dew_risk"] = bool(len(dark_spread) and (dark_spread < 2).mean() > 0.3)
    temps = wx["temperature_2m"][dark & ~np.isnan(wx["temperature_2m"])]
    night["min_temp"] = int(round(float(temps.min()))) if len(temps) else None

    if tgt is not None:
        in_dark = np.where(dark, tgt, -90)
        peak = int(np.argmax(in_dark)) if dark.any() else int(np.argmax(tgt))
        night["target"] = {
            "peak_alt": round(float(tgt[peak]), 1),
            "peak_time": _hhmm(t[peak], tz),
            "transit": _hhmm(t[int(np.argmax(tgt))], tz),
            "hours_above_min": round(float((dark & (tgt >= s["min_alt"])).sum()) * STEP_MIN / 60, 1),
        }
    else:
        night["target"] = None

    # Score: long unbroken blocks matter most (stacking), then how much of the dark
    # time is usable, then how clean those hours are.
    if usable:
        length = min(1.0, longest_h / 4)
        coverage = usable / max(len(dark_idx) * STEP_MIN, 1)
        clarity = 1 - (night["avg_cloud"] or 0) / max(s["max_cloud"], 1) * 0.6
        moon_pen = 1 - 0.35 * float(((moon > MOON_HORIZON) & shoot).sum() / max(shoot.sum(), 1)) * float(illum[mid])
        penalty = (0.9 if night["dew_risk"] else 1.0) * (0.85 if (night["avg_wind"] or 0) > 25 else 1.0)
        if darkness == "nautical":
            penalty *= 0.6
        score = 100 * (0.5 * length + 0.3 * coverage + 0.2 * clarity) * moon_pen * penalty
        night["score"] = int(round(max(1, min(100, score))))
    else:
        night["score"] = 0
    night["verdict"] = _verdict(night["score"])
    night["reasons"] = _reasons(night, dark, future, have_wx, cloud_ok, precip_ok, moon_ok, tgt_ok, cloud, precip, s, target, tz, t, moon)

    # Chart timeline: an hour either side of sunset/sunrise.
    below = np.flatnonzero(sun < 0)
    lo = max(0, (below[0] if len(below) else 0) - 6)
    hi = min(len(t), (below[-1] + 7) if len(below) else len(t))
    rnd = lambda arr: [None if np.isnan(v) else round(float(v), 1) for v in arr[lo:hi]]  # noqa: E731
    night["timeline"] = {
        "t": [int(v) for v in t[lo:hi]],
        "labels": [_hhmm(v, tz) for v in t[lo:hi]],
        "sun": rnd(sun),
        "moon": rnd(moon),
        "target": rnd(tgt) if tgt is not None else None,
        "cloud": rnd(cloud),
        "low": rnd(wx["cloud_cover_low"]),
        "mid": rnd(wx["cloud_cover_mid"]),
        "high": rnd(wx["cloud_cover_high"]),
        "precip": rnd(precip),
        "temp": rnd(wx["temperature_2m"]),
        "dew": rnd(wx["dew_point_2m"]),
        "wind": rnd(wx["wind_speed_10m"]),
        "shoot": [bool(v) for v in shoot[lo:hi]],
        "dark_level": dark_level,
    }
    return night


def _span(mask, t, tz, most=2):
    """The stretches where `mask` holds, e.g. '20:40–21:00 and 00:00–05:50'."""
    runs = [f"{_hhmm(t[a], tz)}–{_hhmm(t[b - 1] + sky.STEP_SECONDS, tz)}" for a, b in _blocks(mask)]
    if len(runs) > most:
        runs = runs[:most] + [f"{len(runs) - most} more"]
    return " and ".join([", ".join(runs[:-1]), runs[-1]]) if len(runs) > 1 else (runs[0] if runs else "")


def _reasons(n, dark, future, have_wx, cloud_ok, precip_ok, moon_ok, tgt_ok, cloud, precip, s, target, tz, t, moon):
    out = []
    if n["darkness"] == "none":
        return ["The sun never gets 12° below the horizon — no real darkness tonight."]
    if n["darkness"] == "nautical":
        out.append("No astronomical darkness (sun stays above −18°); using nautical twilight.")
    usable_dark = dark & future
    if not usable_dark.any():
        return out + ["This night is already over."]
    base = usable_dark & have_wx
    if not base.any():
        return out + ["Beyond the reach of the weather forecast."]

    tname = target["short"] if target["id"] != "none" else None
    if n["usable_minutes"]:
        out.append(f"{n['usable_hours']:g} h shootable, longest run {n['best_window']}.")
    fails = {
        "cloud": float((base & ~cloud_ok).mean()),
        "precip": float((base & ~precip_ok).mean()),
        "moon": float((base & ~moon_ok).mean()),
        "target": float((base & ~tgt_ok).mean()),
    }
    for key, frac in sorted(fails.items(), key=lambda kv: -kv[1]):
        if frac < 0.15:
            continue
        if key == "cloud":
            if frac > 0.8:
                out.append(f"Cloudy: {int(np.nanmean(cloud[base]))}% average cover while dark.")
            else:
                out.append(f"Cloud over {s['max_cloud']}% {_span(base & ~cloud_ok, t, tz)}.")
        elif key == "precip":
            out.append(f"Rain risk up to {int(np.nanmax(precip[base]))}% {_span(base & ~precip_ok, t, tz)}.")
        elif key == "moon":
            out.append(f"Moon {n['moon']['illumination']}% lit and up {_span(usable_dark & (moon > MOON_HORIZON), t, tz)}.")
        elif key == "target" and tname:
            if n["target"] and n["target"]["peak_alt"] < s["min_alt"]:
                out.append(f"{tname} peaks at only {n['target']['peak_alt']:g}° while dark (min {s['min_alt']}°).")
            else:
                out.append(f"{tname} is above {s['min_alt']}° only {_span(usable_dark & tgt_ok, t, tz)}.")
    if n["dew_risk"]:
        out.append("Dew likely — run a dew heater.")
    if (n["avg_wind"] or 0) > 25:
        out.append(f"Windy ({n['avg_wind']} km/h) — expect tracking errors.")
    if not n["usable_minutes"] and len(out) == 0:
        out.append("No window meets your limits.")
    return out

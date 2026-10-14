"""
Turns weather + ephemeris into a ranked plan of shootable nights.

Each 10-minute slot that passes your limits is weighted by how much it's worth for imaging
(see skyquality.py): for deep-sky targets, signal-to-noise relative to a clear moonless sky
at this site's zenith; for planets and the Moon, seeing- and altitude-limited resolution.
A night's "effective hours" is the sum of those weights, and its score follows from it.
"""
import math
import time
import warnings
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np

from . import db, geo, horizons, lightpollution, sky, skyquality, targets, weather

DEFAULTS = {"max_cloud": 25, "max_precip": 15, "min_alt": 30, "moon_limit": 1.5}
LIMITS = {"max_cloud": (0, 100), "max_precip": (0, 100), "min_alt": (0, 80), "moon_limit": (0.1, 5.0)}
ASTRO_DARK = -18.0
NAUTICAL_DARK = -12.0
CIVIL_DARK = -6.0  # planets and the Moon can be imaged from the end of civil twilight
SUNSET_ALT = -0.833
# Topocentric altitude of the moon's centre when its upper limb is on the horizon:
# 34' refraction + ~15.5' semi-diameter (parallax is already in astropy's topocentric position).
MOON_HORIZON = -0.833
STEP_MIN = sky.STEP_SECONDS // 60
NIGHTS = 15
MIN_RUN_MIN = 30  # shorter clear gaps aren't worth counting: setup, focus and cloud edges eat them
SCORE_SCALE_H = 2.5  # score = 100·(1 − e^(−hours/2.5)): 0.75 h → 26, 2 h → 55, 4 h → 80, 6 h → 91
# Nights that only reach nautical twilight (high latitudes in summer): the sky is still lit by the
# sun. Twilight sky brightness isn't modelled, so these slots count at a flat, conservative 60%.
NAUTICAL_WEIGHT = 0.6
DEW_LIKELY_C, DEW_POSSIBLE_C = 3.0, 5.0  # air within this of its dew point; clear-sky surfaces run 2–4 °C colder
GUST_WARN_KMH = 30


def clean_settings(raw):
    out = {}
    for key, default in DEFAULTS.items():
        try:
            value = float(raw.get(key, default))
        except (TypeError, ValueError):
            value = default
        if not math.isfinite(value):
            value = default
        lo, hi = LIMITS[key]
        value = min(hi, max(lo, value))
        out[key] = round(value, 1) if isinstance(default, float) else int(value)
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


def _ensemble_on_grid(ens, t, max_cloud):
    """
    Per 10-minute slot, from every ensemble member: the chance that cloud stays within
    `max_cloud` (each model weighted equally, so ECMWF's 51 members don't drown out the
    rest), how many models and members cover the slot, and the 10th–90th percentile of
    cloud cover across all members (the model spread).
    """
    if not ens:
        return None
    times = np.asarray(ens["time"], dtype=float)
    mid = t + sky.STEP_SECONDS / 2
    prob_sum, n_models, n_members = np.zeros(len(t)), np.zeros(len(t), int), np.zeros(len(t), int)
    present, stacks = {}, []
    for name, members in ens["models"].items():
        grid = np.array([_interp_instant(times, _series(m), mid) for m in members])
        have = ~np.isnan(grid)
        count = have.sum(0)
        clear = (have & (np.nan_to_num(grid, nan=101) <= max_cloud)).sum(0)
        covered = count > 0
        prob_sum[covered] += clear[covered] / count[covered]
        n_models += covered
        n_members += count
        present[name] = covered
        stacks.append(grid)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # slots no member reaches
        lo, hi = np.nanpercentile(np.vstack(stacks), [10, 90], axis=0)
        chance = np.where(n_models > 0, prob_sum / np.maximum(n_models, 1), np.nan)
    return {"chance": chance, "models": n_models, "members": n_members, "present": present, "lo": lo, "hi": hi}


def _confidence(ens, longest, sky_ok):
    """
    How likely the night's best window really is clear (or, with no window, how likely
    the dark, moon- and target-OK hours clear up), from the ensemble chance.
    """
    if ens is None:
        return None
    ref = np.zeros(len(sky_ok), bool)
    if longest:
        ref[longest[0]:longest[1]] = True
    else:
        ref = sky_ok.copy()
    ref &= ~np.isnan(ens["chance"])
    if not ref.any():
        return None
    chance = float(ens["chance"][ref].mean())
    models = [name for name, cov in ens["present"].items() if cov[ref].all()]
    level = None
    if longest:
        # One model alone (only GEM reaches past ~day 11) can't show agreement.
        level = "High" if chance >= 0.7 and len(models) >= 2 else "Medium" if chance >= 0.4 and len(models) >= 2 else "Low"
    return {
        "chance": int(round(chance * 100)),
        "level": level,
        "basis": "window" if longest else "dark",
        "models": models,
        "members": int(ens["members"][ref].min()),
    }


def _confidence_reason(c):
    if not c:
        return None
    runs = f"{c['members']} forecast runs ({', '.join(c['models'])})" if c["models"] else "forecast runs"
    if c["basis"] == "dark":
        return f"{c['chance']}% of {runs} show clear dark hours — worth checking again." if c["chance"] >= 30 else None
    lone = " Only one model reaches this far ahead." if len(c["models"]) < 2 else ""
    if c["level"] == "High":
        return f"High confidence: {c['chance']}% of {runs} keep the best window clear."
    if c["level"] == "Medium":
        return f"Models are split: {c['chance']}% of {runs} keep the best window clear.{lone}"
    return f"Low confidence: only {c['chance']}% of {runs} keep the best window clear.{lone}"


def _verdict(score):
    if score >= 75:
        return "Excellent"
    if score >= 50:
        return "Good"
    if score >= 25:
        return "Marginal"
    return "Poor"


def _tip(target, sky_site):
    bortle = sky_site.get("bortle")
    if not bortle or target["kind"] == "none" or target["mode"] == "planetary":
        return None
    kind = target["type"]
    if bortle >= 6 and kind in ("Emission nebula", "Supernova remnant", "Planetary nebula", "Cluster + nebula"):
        return f"Bortle {bortle} sky: a dual-band (Hα/OIII) filter will cut most of the light pollution."
    if bortle >= 7 and kind in ("Galaxy", "Galaxy group", "Reflection nebula", "Dark nebula", "Comet"):
        return f"Bortle {bortle} sky: {kind.lower()}s need long integration here, or a trip to a darker site."
    if bortle >= 7 and kind == "Milky Way":
        return f"Bortle {bortle} sky: the Milky Way core will be washed out; find a darker site."
    return None


def _site_sky(lat, lon):
    """Zenith sky brightness: your saved Bortle class if you set one, else the satellite atlas."""
    manual = db.bortle_for(lat, lon)
    atlas = lightpollution.lookup(lat, lon)
    if manual:
        return {"sqm": skyquality.BORTLE_TO_SQM[manual], "bortle": manual, "source": "your setting",
                "atlas": atlas}
    if atlas:
        return {**atlas, "atlas": atlas}
    return {"sqm": 21.0, "bortle": 4, "source": "assumed (atlas unavailable)", "atlas": None}


def _aerosol_on_grid(aer, t):
    """AOD per slot; past the ~5-day forecast, use the forecast's median for this place."""
    if not aer:
        return None
    times = np.asarray(aer["time"], dtype=float)
    vals = _series(aer["aod"])
    grid = _interp_instant(times, vals, t + sky.STEP_SECONDS / 2)
    known = vals[~np.isnan(vals)]
    if len(known):
        grid = np.where(np.isnan(grid), float(np.median(known)), grid)
    return grid


def _target_track(target, lat, lon, elevation, start, count):
    """(target, alt, az) on the grid; target may gain a resolved name. alt is None for 'none'."""
    key = (round(lat, 4), round(lon, 4), round(elevation))
    if target["kind"] == "none":
        return target, None, None
    if target["kind"] == "fixed":
        alt, az = sky.fixed_altaz(*key, target["ra"], target["dec"], start, count)
        return target, alt, az
    if target["kind"] == "body":
        alt, az = sky.body_altaz(*key, target["id"][5:], start, count)
        return target, alt, az
    try:
        name, th, ra, dec = horizons.track(target["command"], lat, lon, elevation, start, start + count * sky.STEP_SECONDS)
    except horizons.HorizonsError as err:
        raise geo.LookupError_(f"JPL Horizons: {err}") from err
    ra_g, dec_g = horizons.on_grid(th, ra, dec, sky.grid(start, count))
    alt, az = sky.track_altaz(*key, ra_g, dec_g, start, count)
    if name:
        target = {**target, "name": name, "short": name}
    return target, alt, az


def plan(location, target_id="none", settings=None, now=None):
    """
    location: {label, lat, lon, elevation?, timezone?}
    Returns every night in the forecast with effective hours, a score, windows and a chart timeline.
    """
    s = clean_settings(settings or {})
    target = targets.get(target_id)
    lat, lon = float(location["lat"]), float(location["lon"])
    wx = weather.fetch(lat, lon)
    # Browsers rarely report altitude; fall back to the forecast grid's terrain height.
    elevation = float(location.get("elevation") or wx.get("elevation") or 0)
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

    sm = sky.sun_moon(round(lat, 4), round(lon, 4), round(elevation), start, count)
    target, tgt_alt, tgt_az = _target_track(target, lat, lon, elevation, start, count)
    wxg = _weather_on_grid(wx, t)
    ensg = _ensemble_on_grid(weather.fetch_ensemble(lat, lon), t, s["max_cloud"])
    aod = _aerosol_on_grid(weather.fetch_aerosol(lat, lon), t)
    k = skyquality.extinction_coefficient(elevation, aod) * np.ones(len(t))
    site = _site_sky(lat, lon)

    nights = []
    for i in range(NIGHTS):
        a = int(np.searchsorted(t, noons[i]))
        b = int(np.searchsorted(t, noons[i + 1]))
        cut = lambda arr: None if arr is None else arr[a:b]  # noqa: E731
        nights.append(_night(
            first + timedelta(days=i), t[a:b], {key: v[a:b] for key, v in sm.items()}, cut(tgt_alt), cut(tgt_az),
            {key: v[a:b] for key, v in wxg.items()}, _slice(ensg, a, b), k[a:b], cut(aod), site, s, target, tz, now))

    # Before noon the list starts with last night; drop it once its darkness is over.
    if nights and nights[0]["reasons"] == ["This night is already over."]:
        nights = nights[1:]

    # Best night = highest score weighted by how likely its window is to stay clear, so a
    # confident 70 beats a 90 that only a third of the forecast runs agree with.
    def trust(n):
        if ensg is None:
            return 1.0
        return n["confidence"]["chance"] / 100 if n["confidence"] else 0.3

    ranked = [n for n in nights if n["usable_minutes"] > 0]
    best = max(ranked, key=lambda n: (n["score"] * trust(n), n["score"], n["usable_minutes"]), default=None)
    planetary = target["mode"] == "planetary"
    return {
        "location": {**location, "lat": lat, "lon": lon, "elevation": elevation, "timezone": str(tz.key),
                     "bortle": site["bortle"], "sqm": site["sqm"], "sky_source": site["source"],
                     "atlas": site.get("atlas")},
        "target": {key: v for key, v in target.items() if key != "command"},
        "settings": s,
        "metric": {
            "label": "seeing-weighted hours" if planetary else "dark-sky hours",
            "explain": ("Clear hours weighted by altitude and forecast seeing (jet stream), relative to the "
                        "object overhead in steady air." if planetary else
                        "Clear hours weighted by moonlight, altitude and haze, relative to the same time with "
                        "the target overhead on a clear, moonless night at this site."),
        },
        "generated_at": int(now),
        "weather_fetched_at": wx.get("fetched_at"),
        "nights": nights,
        "best": best and {key: best[key] for key in ("date", "label", "best_window", "score", "verdict", "usable_hours",
                                                      "effective_hours", "confidence")},
        "ensemble": ensg is not None,
        "aerosol": aod is not None,
        "good_nights": sum(1 for n in nights if n["score"] >= 50),
        "tip": _tip(target, site),
    }


def _slice(ens, a, b):
    if ens is None:
        return None
    return {key: ({n: c[a:b] for n, c in v.items()} if isinstance(v, dict) else v[a:b]) for key, v in ens.items()}


def _drop_short_runs(mask, min_slots):
    out = mask.copy()
    for a, b in _blocks(mask):
        if b - a < min_slots:
            out[a:b] = False
    return out


def _night(day, t, sm, tgt, tgt_az, wx, ens, k, aod, site, s, target, tz, now):
    night = {"date": day.isoformat(), "label": f"{day:%a %b} {day.day}"}
    sun, moon = sm["sun_alt"], sm["moon_alt"]
    planetary = target["mode"] == "planetary"

    sunsets = _crossings(sun, SUNSET_ALT, t)[1]
    sunrises = _crossings(sun, SUNSET_ALT, t)[0]
    night["sunset"] = _hhmm(sunsets[0], tz) if sunsets else None
    night["sunrise"] = _hhmm(sunrises[-1], tz) if sunrises else None
    night["polar"] = "polar night" if (sun < SUNSET_ALT).all() else "midnight sun" if (sun > SUNSET_ALT).all() else None

    if planetary:
        dark_level, darkness = CIVIL_DARK, "civil"
        dark = sun <= CIVIL_DARK
        if not dark.any():
            darkness = "none"
    else:
        dark_level, darkness = ASTRO_DARK, "astronomical"
        dark = sun <= ASTRO_DARK
        if not dark.any():
            dark_level, dark, darkness = NAUTICAL_DARK, sun <= NAUTICAL_DARK, "nautical"
            if not dark.any():
                darkness = "none"
    night["darkness"] = darkness
    dark_idx = np.flatnonzero(dark)
    dusk, dawn = _crossings(sun, dark_level, t)[1], _crossings(sun, dark_level, t)[0]
    # In polar night darkness has no start or end; report the edges of this noon-to-noon span.
    night["dark_start"] = _hhmm(dusk[0] if dusk else t[dark_idx[0]], tz) if len(dark_idx) else None
    night["dark_end"] = _hhmm(dawn[-1] if dawn else t[dark_idx[-1]] + sky.STEP_SECONDS, tz) if len(dark_idx) else None
    night["dark_hours"] = round(len(dark_idx) * STEP_MIN / 60, 1)

    mid = dark_idx[len(dark_idx) // 2] if len(dark_idx) else len(t) // 2
    night_span = sun < 0
    m_rises, m_sets = (
        [x for x in ev if night_span[min(int(np.searchsorted(t, x)), len(t) - 1)]] for ev in _crossings(moon, MOON_HORIZON, t)
    )
    night["moon"] = {
        "phase": sky.phase_name(sm["elong"][mid]),
        "illumination": int(round(sm["illum"][mid] * 100)),
        "elongation": round(float(sm["elong"][mid]), 1),
        "rise": _hhmm(m_rises[0], tz) if m_rises else None,
        "set": _hhmm(m_sets[0], tz) if m_sets else None,
        "up_fraction": round(float((moon[dark] > MOON_HORIZON).mean()) if dark.any() else 0.0, 2),
    }

    # Where we look: the target, or the zenith for wide-field / sky-only plans.
    look_alt = np.full(len(t), 90.0) if tgt is None else tgt
    look_az = np.zeros(len(t)) if tgt_az is None else tgt_az
    sep = skyquality.angular_separation(look_alt, look_az, moon, sm["moon_az"])
    moon_up = moon > MOON_HORIZON
    moon_mag = np.where(moon_up, skyquality.moon_brightening_mag(site["sqm"], moon, sm["phase_angle"], sep, np.clip(look_alt, 0.5, 90), k), 0.0)
    if tgt is not None:
        night["moon"]["separation"] = round(float(sep[mid]), 0)

    cloud = wx["cloud_cover"]
    precip = wx["precipitation_probability"]
    jet = wx["wind_speed_250hPa"]
    gust = wx["wind_gusts_10m"]
    have_wx = ~np.isnan(cloud)
    future = t + sky.STEP_SECONDS > now
    cloud_ok = have_wx & (np.nan_to_num(cloud, nan=101) <= s["max_cloud"])
    # A missing precipitation value alone (common at the far end of the forecast) doesn't veto a slot.
    precip_ok = np.isnan(precip) | (precip <= s["max_precip"])
    # Planets and the Moon cut through moonlight; for everything else the moon counts by how much
    # it actually brightens the sky around the target (Krisciunas & Schaefer), not by phase alone.
    moon_ok = np.ones(len(t), bool) if planetary else moon_mag <= s["moon_limit"]
    tgt_ok = np.ones(len(t), bool) if tgt is None else tgt >= s["min_alt"]
    raw_shoot = dark & future & cloud_ok & precip_ok & moon_ok & tgt_ok
    shoot = _drop_short_runs(raw_shoot, MIN_RUN_MIN // STEP_MIN)

    # Per-slot value (0–1) of imaging time; see skyquality.py.
    if planetary:
        weight = skyquality.planetary_weight(look_alt, k, cloud, jet)
        f_alt = f_moon = None
        f_clear = 1 - np.clip(np.nan_to_num(cloud, nan=100), 0, 100) / 100
    else:
        f_alt, f_moon, f_clear = skyquality.deep_sky_components(site["sqm"], np.clip(look_alt, 0.5, 90), moon, sm["phase_angle"], sep, k, cloud)
        weight = f_alt * f_moon * f_clear
        if darkness == "nautical":
            weight = weight * NAUTICAL_WEIGHT
    weight = np.where(shoot, weight, 0.0)

    runs = _blocks(shoot)
    longest = max(runs, key=lambda r: r[1] - r[0], default=None)
    usable = int(shoot.sum()) * STEP_MIN
    night["usable_minutes"] = usable
    night["usable_hours"] = round(usable / 60, 1)
    effective = float(weight.sum()) * STEP_MIN / 60
    night["effective_hours"] = round(effective, 1)
    lo_clip = dusk[0] if dusk else -np.inf
    hi_clip = dawn[-1] if dawn else np.inf

    def window(x, y):
        return f"{_hhmm(max(t[x], lo_clip), tz)}–{_hhmm(min(t[y - 1] + sky.STEP_SECONDS, hi_clip), tz)}"

    night["windows"] = [window(x, y) for x, y in runs]
    night["best_window"] = window(*longest) if longest else None
    night["longest_hours"] = round((longest[1] - longest[0]) * STEP_MIN / 60, 1) if longest else 0

    ref = shoot if shoot.any() else (dark & have_wx)

    def avg(arr, digits=0):
        vals = arr[ref & ~np.isnan(arr)]
        if not len(vals):
            return None
        v = float(vals.mean())
        return int(round(v)) if digits == 0 else round(v, digits)

    night["avg_cloud"] = avg(cloud)
    night["clouds"] = {"low": avg(wx["cloud_cover_low"]), "mid": avg(wx["cloud_cover_mid"]), "high": avg(wx["cloud_cover_high"])}
    night["avg_precip"] = avg(precip)
    night["avg_wind"] = avg(wx["wind_speed_10m"])
    gust_ref = gust[(shoot if shoot.any() else dark) & ~np.isnan(gust)]
    night["max_gust"] = int(round(float(gust_ref.max()))) if len(gust_ref) else None
    temps = wx["temperature_2m"][dark & ~np.isnan(wx["temperature_2m"])]
    night["min_temp"] = int(round(float(temps.min()))) if len(temps) else None

    # Dew: on clear nights optics radiate to the sky and run 2–4 °C below the air, so dew forms
    # well before the air itself reaches its dew point.
    spread = (wx["temperature_2m"] - wx["dew_point_2m"])[dark & ~np.isnan(wx["dew_point_2m"])]
    dew = None
    if len(spread):
        if (spread <= DEW_LIKELY_C).mean() > 0.3:
            dew = "likely"
        elif (spread <= DEW_POSSIBLE_C).mean() > 0.3:
            dew = "possible"
    night["dew"] = dew
    night["dew_risk"] = dew == "likely"

    jet_avg = avg(jet)
    night["sky"] = {
        "sqm": site["sqm"],
        "moon_mag": round(float(moon_mag[ref].max()), 1) if ref.any() and not planetary else None,
        "aod": avg(aod, 2) if aod is not None else None,
        "extinction": avg(k, 2),
        "jet": jet_avg,
        "seeing": skyquality.seeing_label(jet_avg),
        "efficiency": round(effective / (usable / 60), 2) if usable else None,
    }
    if usable and f_alt is not None:
        w = shoot
        night["sky"]["losses"] = {
            "moon": round(1 - float(f_moon[w].mean()), 2),
            "altitude": round(1 - float(f_alt[w].mean()), 2),
            "cloud": round(1 - float(f_clear[w].mean()), 2),
        }

    if tgt is not None:
        in_dark = np.where(dark, tgt, -90)
        peak = int(np.argmax(in_dark)) if dark.any() else int(np.argmax(tgt))
        night["target"] = {
            "peak_alt": round(float(tgt[peak]), 1),
            "peak_time": _hhmm(t[peak], tz),
            "transit": _hhmm(t[int(np.argmax(tgt))], tz),
            "hours_above_min": round(float((dark & (tgt >= s["min_alt"])).sum()) * STEP_MIN / 60, 1),
            "min_airmass": round(float(skyquality.airmass(tgt[peak])), 2) if tgt[peak] > 0 else None,
        }
    else:
        night["target"] = None

    if effective > 0:
        night["score"] = max(1, int(round(100 * (1 - math.exp(-effective / SCORE_SCALE_H)))))
    else:
        night["score"] = 0
    night["verdict"] = _verdict(night["score"])
    night["confidence"] = _confidence(ens, longest, dark & future & moon_ok & tgt_ok)
    night["reasons"] = _reasons(night, dark, future, have_wx, cloud_ok, precip_ok, moon_ok, tgt_ok, raw_shoot, shoot,
                                cloud, precip, moon_mag, gust, s, target, tz, t, planetary)
    why = _confidence_reason(night["confidence"])
    if why:
        after = next((i + 1 for i, r in enumerate(night["reasons"]) if " h shootable" in r), len(night["reasons"]))
        night["reasons"].insert(after, why)

    # Chart timeline: an hour either side of sunset/sunrise (the whole span in polar night/day).
    below = np.flatnonzero(sun < 0)
    lo = max(0, (below[0] if len(below) else 0) - 6)
    hi = min(len(t), (below[-1] + 7) if len(below) else len(t))
    rnd = lambda arr, d=1: [None if np.isnan(v) else round(float(v), d) for v in arr[lo:hi]]  # noqa: E731
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
        "gust": rnd(gust),
        "jet": rnd(jet, 0),
        "moon_mag": None if planetary else rnd(moon_mag, 2),
        "quality": rnd(weight, 2),
        "shoot": [bool(v) for v in shoot[lo:hi]],
        "dark_level": dark_level,
        "ens_lo": rnd(ens["lo"]) if ens else None,
        "ens_hi": rnd(ens["hi"]) if ens else None,
        "clear_chance": [None if np.isnan(v) else int(round(v * 100)) for v in ens["chance"][lo:hi]] if ens else None,
    }
    return night


def _span(mask, t, tz, most=2):
    """The stretches where `mask` holds, e.g. '20:40–21:00 and 00:00–05:50'."""
    runs = [f"{_hhmm(t[a], tz)}–{_hhmm(t[b - 1] + sky.STEP_SECONDS, tz)}" for a, b in _blocks(mask)]
    if len(runs) > most:
        runs = runs[:most] + [f"{len(runs) - most} more"]
    return " and ".join([", ".join(runs[:-1]), runs[-1]]) if len(runs) > 1 else (runs[0] if runs else "")


def _reasons(n, dark, future, have_wx, cloud_ok, precip_ok, moon_ok, tgt_ok, raw_shoot, shoot, cloud, precip,
             moon_mag, gust, s, target, tz, t, planetary):
    out = []
    if n["polar"] == "polar night":
        out.append("Polar night: the sun stays down all day.")
    if n["darkness"] == "none":
        need = "6° (end of civil twilight)" if planetary else "12°"
        return out + [f"The sun never gets {need} below the horizon — no real darkness tonight."]
    if n["darkness"] == "nautical":
        out.append("No astronomical darkness (sun stays above −18°); using nautical twilight at reduced value.")
    usable_dark = dark & future
    if not usable_dark.any():
        return out + ["This night is already over."]
    base = usable_dark & have_wx
    if not base.any():
        return out + ["Beyond the reach of the weather forecast."]

    tname = target["short"] if target["kind"] != "none" else None
    sky_q = n["sky"]
    if n["usable_minutes"]:
        out.append(f"{n['usable_hours']:g} h shootable, longest run {n['best_window']} — worth "
                   f"{n['effective_hours']:g} {'seeing-weighted' if planetary else 'dark-sky'} hours.")
        losses = sky_q.get("losses") or {}
        worst = max(losses.items(), key=lambda kv: kv[1], default=(None, 0))
        if worst[1] >= 0.2:
            what = {"moon": f"moonlight (sky up to {sky_q['moon_mag']:g} mag brighter)",
                    "altitude": "low altitude (more air to look through)",
                    "cloud": "patchy cloud within your limit"}[worst[0]]
            out.append(f"Biggest loss: {what}, about {int(round(worst[1] * 100))}%.")
    short = raw_shoot & ~shoot
    if short.any() and not shoot.any():
        out.append(f"Only gaps shorter than {MIN_RUN_MIN} min ({_span(short, t, tz)}) — too short to count.")
    fails = {
        "cloud": float((base & ~cloud_ok).mean()),
        "precip": float((base & ~precip_ok).mean()),
        "moon": float((base & ~moon_ok).mean()),
        "target": float((base & ~tgt_ok).mean()),
    }
    # Biggest blocker first; on ties a target that's too low beats the weather (it can't change).
    order = {"target": 0, "moon": 1, "cloud": 2, "precip": 3}
    for key, frac in sorted(fails.items(), key=lambda kv: (-round(kv[1], 2), order[kv[0]])):
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
            where = f"around {tname}" if tname else "overhead"
            peak = float(np.nanmax(np.where(base, moon_mag, 0)))
            out.append(f"Moon ({n['moon']['illumination']}% lit) brightens the sky {where} by up to {peak:.1f} mag "
                       f"(limit {s['moon_limit']:g}) {_span(base & ~moon_ok, t, tz)}.")
        elif key == "target" and tname:
            if n["target"] and n["target"]["peak_alt"] < s["min_alt"]:
                where = "never rises here" if n["target"]["peak_alt"] < 0 else f"peaks at only {n['target']['peak_alt']:g}° while dark"
                out.append(f"{tname} {where} (min {s['min_alt']}°).")
            else:
                out.append(f"{tname} is above {s['min_alt']}° only {_span(usable_dark & tgt_ok, t, tz)}.")
    aod = sky_q.get("aod")
    if aod is not None and aod >= 0.3:
        out.append(f"Hazy air (aerosol optical depth {aod:.2f}: smoke, dust or pollution) — targets dimmed, light pollution scattered more.")
    if sky_q.get("seeing") == "poor":
        out.append(f"Jet stream {sky_q['jet']} km/h overhead — poor seeing{'; planetary detail will suffer' if planetary else ', stars will be soft'}.")
    elif planetary and sky_q.get("seeing") == "good":
        out.append(f"Calm upper air (jet stream {sky_q['jet']} km/h) — good seeing likely.")
    if n["dew"] == "likely":
        out.append(f"Dew likely (air within {DEW_LIKELY_C:g} °C of its dew point) — run a dew heater.")
    elif n["dew"] == "possible":
        out.append("Dew possible later in the night — have a dew heater ready.")
    if n["max_gust"] is not None and n["max_gust"] > GUST_WARN_KMH:
        windy = (shoot if shoot.any() else dark) & (np.nan_to_num(gust) > GUST_WARN_KMH)
        out.append(f"Gusts to {n['max_gust']} km/h {_span(windy, t, tz)} — expect tracking errors; shield the rig.")
    if not n["usable_minutes"] and len(out) == 0:
        out.append("No window meets your limits.")
    return out

import time

import requests

from . import db
from .geo import LookupError_

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
HOURLY = [
    "cloud_cover", "cloud_cover_low", "cloud_cover_mid", "cloud_cover_high",
    "precipitation_probability", "temperature_2m", "dew_point_2m",
    "relative_humidity_2m", "wind_speed_10m", "wind_gusts_10m", "visibility",
    "wind_speed_250hPa",  # jet stream: the main forecastable driver of bad seeing
]
# Hourly values that cover the hour *before* their timestamp; everything else is instantaneous.
PRECEDING_HOUR = {"precipitation_probability", "wind_gusts_10m"}
CACHE_SECONDS = 30 * 60


def fetch(lat, lon):
    """16-day hourly forecast. Times are unix seconds (UTC), so no DST ambiguity."""
    key = f"wx2:{round(lat, 3)},{round(lon, 3)}"
    cached = db.cache_get(key, CACHE_SECONDS)
    if cached:
        return cached
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": ",".join(HOURLY),
        "timezone": "auto",
        "timeformat": "unixtime",
        "forecast_days": 16,
        "wind_speed_unit": "kmh",
    }
    try:
        res = requests.get(FORECAST_URL, params=params, timeout=15)
        res.raise_for_status()
    except requests.RequestException as exc:
        raise LookupError_(f"Weather forecast unavailable: {exc}") from exc
    data = res.json()
    if "hourly" not in data:
        raise LookupError_(data.get("reason") or "Weather forecast returned no data")
    data["fetched_at"] = int(time.time())
    db.cache_put(key, data)
    return data


ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
# Independent ensembles, so their spread reflects real forecast uncertainty.
# Reach (days): ECMWF ~11-15, GFS ~10, ICON ~7.5, GEM 16.
ENSEMBLE_MODELS = {
    "ecmwf_ifs025": "ECMWF",
    "gfs025": "GFS",
    "icon_seamless": "ICON",
    "gem_global": "GEM",
}
ENSEMBLE_CACHE_SECONDS = 60 * 60


def fetch_ensemble(lat, lon):
    """
    Hourly total cloud cover from every member of several ensembles, grouped by model:
    {"time": [...], "models": {"ECMWF": [[member cloud %, ...], ...], ...}}.
    Returns None if the ensemble service is unavailable; confidence is optional.
    """
    key = f"ens:{round(lat, 3)},{round(lon, 3)}"
    cached = db.cache_get(key, ENSEMBLE_CACHE_SECONDS)
    if cached:
        return cached
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "cloud_cover",
        "models": ",".join(ENSEMBLE_MODELS),
        "timeformat": "unixtime",
        "forecast_days": 16,
    }
    try:
        res = requests.get(ENSEMBLE_URL, params=params, timeout=20)
        res.raise_for_status()
        hourly = res.json()["hourly"]
    except (requests.RequestException, ValueError, KeyError):
        return None
    models = {}
    for series, values in hourly.items():
        for api_name, label in ENSEMBLE_MODELS.items():
            # Keys look like cloud_cover_ecmwf_ifs025_ensemble (control) and
            # cloud_cover_member07_ecmwf_ifs025_ensemble; gfs025 comes back as ncep_gefs025.
            if api_name in series or (api_name == "gfs025" and "gefs025" in series):
                if any(v is not None for v in values):
                    models.setdefault(label, []).append(values)
    if not models:
        return None
    data = {"time": hourly["time"], "models": models, "fetched_at": int(time.time())}
    db.cache_put(key, data)
    return data


AIR_QUALITY_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"


def fetch_aerosol(lat, lon):
    """
    Hourly aerosol optical depth at 550 nm (CAMS, ~5 days ahead): haze, smoke and dust that
    dim targets and scatter light pollution. Returns {"time", "aod"} or None if unavailable.
    """
    key = f"aod:{round(lat, 2)},{round(lon, 2)}"
    cached = db.cache_get(key, ENSEMBLE_CACHE_SECONDS)
    if cached:
        return cached
    params = {"latitude": lat, "longitude": lon, "hourly": "aerosol_optical_depth", "timeformat": "unixtime", "forecast_days": 7}
    try:
        res = requests.get(AIR_QUALITY_URL, params=params, timeout=15)
        res.raise_for_status()
        hourly = res.json()["hourly"]
    except (requests.RequestException, ValueError, KeyError):
        return None
    if not any(v is not None for v in hourly.get("aerosol_optical_depth", [])):
        return None
    data = {"time": hourly["time"], "aod": hourly["aerosol_optical_depth"]}
    db.cache_put(key, data)
    return data

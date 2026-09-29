import json
import time

import requests

from . import db
from .geo import LookupError_

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
HOURLY = [
    "cloud_cover", "cloud_cover_low", "cloud_cover_mid", "cloud_cover_high",
    "precipitation_probability", "temperature_2m", "dew_point_2m",
    "relative_humidity_2m", "wind_speed_10m", "visibility",
]
CACHE_SECONDS = 30 * 60


def fetch(lat, lon):
    """16-day hourly forecast. Times are unix seconds (UTC), so no DST ambiguity."""
    key = f"{round(lat, 3)},{round(lon, 3)}"
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

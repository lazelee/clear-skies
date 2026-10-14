import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "clear_skies.db"
LEGACY_DB_PATH = DB_PATH.with_name("astro_scheduler.db")  # name before the Clear Skies rename

SCHEMA = """
CREATE TABLE IF NOT EXISTS forecast_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    location TEXT,
    lat REAL,
    lon REAL,
    target TEXT,
    settings TEXT,
    good_nights INTEGER,
    best_date TEXT,
    best_window TEXT,
    best_score INTEGER
);
CREATE TABLE IF NOT EXISTS night_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER REFERENCES forecast_runs(id) ON DELETE CASCADE,
    date TEXT,
    score INTEGER,
    verdict TEXT,
    best_window TEXT,
    usable_hours REAL,
    avg_cloud_cover REAL,
    moon_phase TEXT,
    moon_illumination INTEGER,
    target_peak_alt REAL
);
CREATE TABLE IF NOT EXISTS saved_locations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT,
    lat REAL,
    lon REAL,
    elevation REAL,
    timezone TEXT,
    bortle INTEGER,
    UNIQUE(lat, lon)
);
CREATE TABLE IF NOT EXISTS weather_cache (
    key TEXT PRIMARY KEY,
    fetched_at INTEGER,
    payload TEXT
);
"""


def _migrate_legacy_db():
    """Carry saved locations, history and caches over from the pre-rename database file."""
    if DB_PATH.exists() or not LEGACY_DB_PATH.exists():
        return
    for suffix in ("", "-wal", "-shm", "-journal"):
        old = LEGACY_DB_PATH.with_name(LEGACY_DB_PATH.name + suffix)
        if old.exists():
            old.rename(DB_PATH.with_name(DB_PATH.name + suffix))


def connect():
    _migrate_legacy_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn




def init():
    with closing(connect()) as conn, conn:
        conn.executescript(SCHEMA)


def cache_get(key, max_age):
    with closing(connect()) as conn:
        row = conn.execute("SELECT fetched_at, payload FROM weather_cache WHERE key = ?", (key,)).fetchone()
    if row and time.time() - row["fetched_at"] < max_age:
        return json.loads(row["payload"])
    return None


def cache_put(key, payload):
    with closing(connect()) as conn, conn:
        conn.execute(
            "INSERT OR REPLACE INTO weather_cache (key, fetched_at, payload) VALUES (?, ?, ?)",
            (key, int(time.time()), json.dumps(payload)),
        )


def log_run(plan):
    loc = plan["location"]
    best = plan.get("best")
    with closing(connect()) as conn, conn:
        cur = conn.execute(
            """INSERT INTO forecast_runs (location, lat, lon, target, settings, good_nights, best_date, best_window, best_score)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                loc["label"], loc["lat"], loc["lon"], plan["target"]["name"], json.dumps(plan["settings"]),
                plan["good_nights"], best and best["date"], best and best["best_window"], best and best["score"],
            ),
        )
        run_id = cur.lastrowid
        conn.executemany(
            """INSERT INTO night_results (run_id, date, score, verdict, best_window, usable_hours, avg_cloud_cover,
                                          moon_phase, moon_illumination, target_peak_alt)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (run_id, n["date"], n["score"], n["verdict"], n["best_window"], n["usable_hours"], n["avg_cloud"],
                 n["moon"]["phase"], n["moon"]["illumination"], n["target"] and n["target"]["peak_alt"])
                for n in plan["nights"]
            ],
        )
    return run_id


def history(limit=50):
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM forecast_runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def locations():
    with closing(connect()) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM saved_locations ORDER BY label").fetchall()]


def save_location(label, lat, lon, elevation=0, timezone=None, bortle=None):
    lat, lon = round(float(lat), 4), round(float(lon), 4)
    with closing(connect()) as conn, conn:
        conn.execute(
            """INSERT INTO saved_locations (label, lat, lon, elevation, timezone, bortle) VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(lat, lon) DO UPDATE SET label = excluded.label, bortle = excluded.bortle,
                   elevation = excluded.elevation, timezone = excluded.timezone""",
            (label, lat, lon, elevation, timezone, bortle),
        )


def delete_location(loc_id):
    with closing(connect()) as conn, conn:
        conn.execute("DELETE FROM saved_locations WHERE id = ?", (loc_id,))


def bortle_for(lat, lon):
    with closing(connect()) as conn:
        row = conn.execute(
            "SELECT bortle FROM saved_locations WHERE lat = ? AND lon = ?", (round(float(lat), 4), round(float(lon), 4))
        ).fetchone()
    return row["bortle"] if row else None

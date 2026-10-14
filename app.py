"""Local web UI for Clear Skies: python app.py → http://localhost:5050"""
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import Flask, Response, jsonify, request, send_from_directory

from clearskies import db, geo, planner, targets

db.init()
app = Flask(__name__, static_folder="static", static_url_path="/static")


def _location_from_args(args):
    try:
        lat = float(args["lat"])
        lon = float(args["lon"])
    except (KeyError, ValueError):
        raise ValueError("lat and lon are required")
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("lat/lon out of range")
    return {
        "label": args.get("label") or f"{lat:.3f}, {lon:.3f}",
        "lat": lat,
        "lon": lon,
        "elevation": float(args.get("elevation") or 0),
        "timezone": args.get("timezone") or None,
    }


def _plan(args):
    return planner.plan(_location_from_args(args), args.get("target", "none"), args)


@app.errorhandler(ValueError)
def bad_request(err):
    return jsonify(error=str(err)), 400


@app.errorhandler(geo.LookupError_)
def upstream(err):
    return jsonify(error=str(err)), 502


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/search")
def search():
    return jsonify(results=geo.search(request.args.get("q", "")))


@app.get("/api/targets")
def target_list():
    return jsonify(targets=targets.as_list(), defaults=planner.DEFAULTS)


@app.get("/api/targets/search")
def target_search():
    """Offline, as-you-type matches from the presets, planets and the 13k-object catalog."""
    return jsonify(results=targets.search(request.args.get("q", "")))


@app.get("/api/targets/resolve")
def target_resolve():
    """Anything else: coordinates, SIMBAD (via CDS Sesame) or JPL Horizons."""
    out = targets.resolve(request.args.get("q", ""))
    return jsonify(out), (200 if "target" in out else 404)


@app.get("/api/forecast")
def forecast():
    plan = _plan(request.args)
    plan["run_id"] = db.log_run(plan)
    return jsonify(plan)


@app.get("/api/outlook")
def outlook():
    """Nights 16–30: astronomy plus climatology (no weather forecast reaches that far)."""
    args = request.args
    return jsonify(planner.outlook(_location_from_args(args), args.get("target", "none"), args))


@app.get("/api/ics")
def ics():
    plan = _plan(request.args)
    night = next((n for n in plan["nights"] if n["date"] == request.args.get("date")), None)
    if not night or not night["best_window"]:
        return jsonify(error="No shootable window that night"), 404
    tz = ZoneInfo(plan["location"]["timezone"])
    day = datetime.fromisoformat(night["date"])
    start_s, end_s = night["best_window"].split("–")

    def at(hhmm):
        h, m = map(int, hhmm.split(":"))
        dt = day.replace(hour=h, minute=m, tzinfo=tz)
        return dt + timedelta(days=1) if h < 12 else dt  # after midnight → next calendar day

    start, end = at(start_s), at(end_s)
    fmt = lambda d: d.astimezone(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")  # noqa: E731
    target = plan["target"]
    safe_id = re.sub(r"[^A-Za-z0-9]+", "-", target["short"] or target["id"]).strip("-")[:40] or "target"
    what = target["name"] if target["kind"] != "none" else "Astrophotography"
    desc = " ".join(night["reasons"]) + f" Moon {night['moon']['illumination']}% ({night['moon']['phase']})."
    body = "\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//clear-skies//EN", "BEGIN:VEVENT",
        f"UID:{night['date']}-{safe_id}-{plan['location']['lat']:.3f}{plan['location']['lon']:.3f}@clear-skies",
        f"DTSTAMP:{fmt(datetime.now(tz))}", f"DTSTART:{fmt(start)}", f"DTEND:{fmt(end)}",
        f"SUMMARY:{what} — {night['verdict']} night ({night['score']}, {night['effective_hours']:g} h effective)",
        f"LOCATION:{plan['location']['label']}",
        "DESCRIPTION:" + desc.replace(",", "\\,").replace(";", "\\;"),
        "BEGIN:VALARM", "TRIGGER:-PT60M", "ACTION:DISPLAY", "DESCRIPTION:Set up the rig", "END:VALARM",
        "END:VEVENT", "END:VCALENDAR", "",
    ])
    name = f"clear-skies-{night['date']}-{safe_id}.ics"
    return Response(body, mimetype="text/calendar", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/api/locations")
def locations():
    return jsonify(locations=db.locations())


@app.post("/api/locations")
def save_location():
    body = request.get_json(silent=True) or {}
    loc = _location_from_args(body)
    bortle = body.get("bortle")
    bortle = int(bortle) if bortle not in (None, "") else None
    if bortle is not None and not 1 <= bortle <= 9:
        raise ValueError("Bortle must be 1–9")
    db.save_location(loc["label"], loc["lat"], loc["lon"], loc["elevation"], loc["timezone"], bortle)
    return jsonify(locations=db.locations())


@app.delete("/api/locations/<int:loc_id>")
def delete_location(loc_id):
    db.delete_location(loc_id)
    return jsonify(locations=db.locations())


@app.get("/api/history")
def history():
    return jsonify(runs=db.history())


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5050"))
    print(f"\n  Clear Skies → http://localhost:{port}\n")
    app.run(host="127.0.0.1", port=port, debug=False)

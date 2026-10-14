"""Command-line run: print the plan for a place and target.

    python pipeline.py --city Toronto --country Canada --target M31
"""
import argparse
import sys

from clearskies import db, geo, planner, targets


def main():
    ap = argparse.ArgumentParser(description="Find the best astrophotography nights in the 16-day forecast.")
    ap.add_argument("--city", default="Toronto")
    ap.add_argument("--country", default="Canada")
    ap.add_argument("--target", default="M31",
                    help="anything: M31, 'Whirlpool', NGC891, Sh2-155, Vega, Jupiter, 'C/2023 A3', '10h41m +41d16m', or 'none'")
    ap.add_argument("--max-cloud", type=int, default=planner.DEFAULTS["max_cloud"])
    ap.add_argument("--max-precip", type=int, default=planner.DEFAULTS["max_precip"])
    ap.add_argument("--min-alt", type=int, default=planner.DEFAULTS["min_alt"])
    ap.add_argument("--moon-limit", type=float, default=planner.DEFAULTS["moon_limit"],
                    help="max sky brightening from moonlight at the target, magnitudes (default 1.5)")
    ap.add_argument("--outlook", action="store_true", help="also show nights 16–30 (astronomy + 15-year climatology)")
    ap.add_argument("--list-targets", action="store_true")
    a = ap.parse_args()

    if a.list_targets:
        for t in targets.as_list():
            print(f"{t['id']:9} {t['name']}  ({t['type']})")
        return 0

    db.init()
    try:
        loc = geo.find(a.city, a.country)
    except geo.LookupError_ as err:
        print(err, file=sys.stderr)
        return 1
    if not loc:
        print(f"Could not find '{a.city}' in '{a.country}'.", file=sys.stderr)
        return 1
    if a.target == "none":
        target_id = "none"
    else:
        found = targets.resolve(a.target)
        if "target" not in found:
            print(found["error"], file=sys.stderr)
            for m in found.get("matches", []):
                print(f"  {m['query']:12} {m['label']}", file=sys.stderr)
            return 1
        target_id = found["target"]["id"]

    settings = {"max_cloud": a.max_cloud, "max_precip": a.max_precip, "min_alt": a.min_alt, "moon_limit": a.moon_limit}
    try:
        plan = planner.plan(loc, target_id, settings)
    except geo.LookupError_ as err:
        print(err, file=sys.stderr)
        return 1
    db.log_run(plan)

    L = plan["location"]
    print(f"\n{loc['label']}  ({loc['lat']:.3f}, {loc['lon']:.3f})  Bortle {L['bortle']} · SQM {L['sqm']:.2f} ({L['sky_source']})"
          f"  ·  target {plan['target']['name']}")
    print(f"Effective hours = {plan['metric']['label']}: {plan['metric']['explain']}\n")
    print(f"{'Night':11} {'Score':>5}  {'Verdict':9} {'Dark':12} {'Best window':12} {'Usable':>6} {'Effect.':>7}  {'Cloud':>5}  {'Sure':>4}  Moon")
    for n in plan["nights"]:
        sure = str(n["confidence"]["chance"]) + "%" if n["confidence"] and n["confidence"]["basis"] == "window" else "—"
        cloud = str(n["avg_cloud"]) + "%" if n["avg_cloud"] is not None else "—"
        print(f"{n['label']:11} {n['score']:>5}  {n['verdict']:9} {(n['dark_start'] or '—') + '–' + (n['dark_end'] or '—'):12} "
              f"{n['best_window'] or '—':12} {n['usable_hours']:>5}h {n['effective_hours']:>6}h  {cloud:>5}  {sure:>4}  "
              f"{n['moon']['phase']} {n['moon']['illumination']}%")
    if plan["best"]:
        b = plan["best"]
        sure = f", {b['confidence']['chance']}% of forecast runs agree" if b["confidence"] else ""
        print(f"\nBest: {b['label']} {b['best_window']} ({b['usable_hours']} h clear = {b['effective_hours']} {plan['metric']['label']}, "
              f"score {b['score']}{sure})")
        night = next(n for n in plan["nights"] if n["date"] == b["date"])
        for r in night["reasons"]:
            print(f"  · {r}")
    else:
        print("\nNo shootable nights in the forecast.")

    if a.outlook:
        o = planner.outlook(loc, target_id, settings)
        clim = f"clear odds from {o['climate']['years']} (ERA5)" if o["climate"] else "no climate data"
        print(f"\nOutlook, nights 16–30 (no forecast this far out; {clim})\n")
        print(f"{'Night':11} {'If clear':>8} {'Dark-sky':>8}  {'Clear odds':>10}  {'Expected':>8}  {'Window':12} Moon")
        for n in o["nights"]:
            odds = f"{n['clear_chance']}%" if n["clear_chance"] is not None else "—"
            exp = f"{n['expected_hours']}h" if n["expected_hours"] is not None else "—"
            print(f"{n['label']:11} {n['usable_hours']:>7}h {n['potential_hours']:>7}h  {odds:>10}  {exp:>8}  {n['window'] or '—':12} "
                  f"{n['moon']['phase']} {n['moon']['illumination']}%")
        if o["best"]:
            b = o["best"]
            stretch = [n["label"] for n in o["nights"] if n["date"] in o["best_dates"]]
            span = stretch[0] if len(stretch) == 1 else f"{stretch[0]} to {stretch[-1]}"
            print(f"\nBest stretch: {span} (within 10% of the best: up to {b['potential_hours']} h if clear, "
                  f"about {b['expected_hours']} h expected)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

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
    ap.add_argument("--target", default="M31", help="target id, e.g. M31, M42, NGC7000, GC, or 'none'")
    ap.add_argument("--max-cloud", type=int, default=planner.DEFAULTS["max_cloud"])
    ap.add_argument("--max-precip", type=int, default=planner.DEFAULTS["max_precip"])
    ap.add_argument("--min-alt", type=int, default=planner.DEFAULTS["min_alt"])
    ap.add_argument("--moon-tol", type=int, default=planner.DEFAULTS["moon_tol"])
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
    if a.target not in targets.BY_ID:
        print(f"Unknown target '{a.target}'. Use --list-targets.", file=sys.stderr)
        return 1

    settings = {"max_cloud": a.max_cloud, "max_precip": a.max_precip, "min_alt": a.min_alt, "moon_tol": a.moon_tol}
    try:
        plan = planner.plan(loc, a.target, settings)
    except geo.LookupError_ as err:
        print(err, file=sys.stderr)
        return 1
    db.log_run(plan)

    bortle = plan["location"]["bortle"]
    print(f"\n{loc['label']}  ({loc['lat']:.3f}, {loc['lon']:.3f})  Bortle {bortle or '?'}  ·  target {plan['target']['name']}\n")
    print(f"{'Night':12} {'Score':>5}  {'Verdict':9} {'Dark':12} {'Best window':12} {'Usable':>6}  {'Cloud':>5}  {'Sure':>4}  Moon")
    for n in plan["nights"]:
        print(f"{n['label']:12} {n['score']:>5}  {n['verdict']:9} {(n['dark_start'] or '—') + '–' + (n['dark_end'] or '—'):12} "
              f"{n['best_window'] or '—':12} {n['usable_hours']:>5}h  {str(n['avg_cloud']) + '%' if n['avg_cloud'] is not None else '—':>5}  "
              f"{str(n['confidence']['chance']) + '%' if n['confidence'] and n['confidence']['basis'] == 'window' else '—':>4}  "
              f"{n['moon']['phase']} {n['moon']['illumination']}%")
    if plan["best"]:
        b = plan["best"]
        sure = f", {b['confidence']['chance']}% of forecast runs agree" if b["confidence"] else ""
        print(f"\nBest: {b['label']} {b['best_window']} ({b['usable_hours']} h, score {b['score']}{sure})")
    else:
        print("\nNo shootable nights in the forecast.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

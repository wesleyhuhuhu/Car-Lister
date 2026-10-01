"""
Look up factory options (via lookup_options.py) for every saved listing that
matches a filter. Defaults: model year 2022 or newer, trim "M3 xDrive Competition".

    python lookup_matching.py --dry-run          # just list what would be looked up
    python lookup_matching.py --model iX         # every 2022+ BMW iX, any trim
    python lookup_matching.py --model iX --trim "xDrive50"   # one trim of it
    python lookup_matching.py                    # look them up
    python lookup_matching.py --limit 5          # only the first 5 this run
    python lookup_matching.py --delay 20         # slower, gentler on the site
    python lookup_matching.py --cooldown 30      # if the site stops, wait 30 min and continue

Nobody knows how many lookups bimmer.work allows, so this is built to be
interrupted: every lookup is saved as soon as it finishes, VINs that already
have a saved lookup are skipped, and when the site stops responding (several
failures in a row) it stops cleanly and tells you how many lookups worked
before that. Run the same command again later and it continues where it left off.
"""
import argparse
import sys
import time
from collections import Counter

from bmw_lookup import add_browser_args, apply_browser_args
from db_push import db_configured
from listing_store import load_rows
from lookup_options import lookup_vins
from vin_router import route_listing


def _norm(text) -> str:
    return " ".join(str(text or "").split()).lower()


def _year(row):
    try:
        return int(str(row.get("year")).strip())
    except (TypeError, ValueError):
        return None


def main():
    parser = argparse.ArgumentParser(description="Look up factory options for all saved listings matching a filter.")
    parser.add_argument("--min-year", type=int, default=2022, help="Only model years at or above this (default 2022)")
    parser.add_argument("--make", default="BMW", help="Make to match (default BMW, the only one lookups support)")
    parser.add_argument("--model", default=None,
                         help='Model to match, ignoring case and spaces, e.g. "iX" or "M3". With --model, any trim matches '
                              "unless --trim is given too")
    parser.add_argument("--trim", default=None,
                         help='Trim to match exactly, ignoring case and extra spaces. Default "M3 xDrive Competition" '
                              'when no --model is given; pass --trim "" to match every trim')
    parser.add_argument("--out-prefix", default="listings")
    parser.add_argument("--no-db", action="store_true", help="Don't push results to the online database")
    parser.add_argument("--dry-run", action="store_true", help="List the matching VINs and exit; nothing is looked up")
    parser.add_argument("--limit", type=int, default=None, help="Look up at most this many VINs in this run")
    parser.add_argument("--delay", type=float, default=10.0,
                         help="Seconds to wait between lookups (default 10; raise it if the site pushes back)")
    parser.add_argument("--max-failures", type=int, default=3,
                         help="Treat the site as stopped after this many failures in a row (default 3)")
    parser.add_argument("--cooldown", type=float, default=0.0,
                         help="Minutes to wait and then try the remaining VINs after the site stops. 0 (default) = just stop")
    parser.add_argument("--max-cooldowns", type=int, default=3,
                         help="How many times --cooldown may be used in one run (default 3)")
    add_browser_args(parser)
    args = parser.parse_args()
    apply_browser_args(args)

    json_path = f"{args.out_prefix}.json"
    rows = load_rows(json_path)
    if not rows:
        sys.exit(f"No saved listings found in {json_path}. Run main.py first.")

    if args.trim is None:
        args.trim = "" if args.model else "M3 xDrive Competition"
    squash = lambda t: _norm(t).replace(" ", "").replace("-", "")
    matches = [r for r in rows
               if r.get("vin")
               and (_year(r) or 0) >= args.min_year
               and (not args.trim or _norm(r.get("trim")) == _norm(args.trim))
               and (not args.model or squash(r.get("model")) == squash(args.model))
               and _norm(r.get("make") or route_listing(title=r.get("title"), vin=r["vin"])[0]) == _norm(args.make)
               and route_listing(title=r.get("title"), vin=r["vin"])[0] == "BMW"]
    pending = [(r["vin"].upper(), r) for r in matches if not r.get("build_sheet")]

    what = ", ".join(x for x in (f"year >= {args.min_year}", f"make {args.make}",
                                 f'model "{args.model}"' if args.model else "", f'trim "{args.trim}"' if args.trim else "any trim") if x)
    print(f"{len(matches)} listings match ({what}): "
          f"{len(matches) - len(pending)} already looked up, {len(pending)} to look up.")

    if not matches:
        found = Counter(f'{r.get("model") or "(blank)"} / {r.get("trim") or "(blank)"}' for r in rows
                        if (_year(r) or 0) >= args.min_year and _norm(r.get("make")) == _norm(args.make))
        if found:
            print(f"{args.make} models / trims found for year >= {args.min_year}:")
            for name, count in found.most_common(15):
                print(f"  {count:4d}  {name}")
        return

    if args.limit is not None:
        pending = pending[:args.limit]
    if args.dry_run:
        for vin, row in pending:
            print(f"  {vin}  {row.get('year')}  {row.get('title')}  {row.get('price')}")
        return
    if not pending:
        print("Nothing left to look up.")
        return

    total_saved = 0
    cooldowns_used = 0
    while pending:
        stats = lookup_vins(rows, pending, args.out_prefix, args.delay, args.max_failures,
                            push_to_db=not args.no_db and db_configured())
        total_saved += stats["saved"]
        pending = stats["remaining"]
        if not pending or not stats["stopped_early"]:
            break
        print(f"bimmer.work stopped responding after {stats['saved']} successful "
              f"lookup(s) in this round.")
        if args.cooldown <= 0 or cooldowns_used >= args.max_cooldowns:
            break
        cooldowns_used += 1
        print(f"Waiting {args.cooldown:g} minutes, then trying the remaining {len(pending)} "
              f"({cooldowns_used}/{args.max_cooldowns}). Progress is saved; Ctrl+C is safe.")
        time.sleep(args.cooldown * 60)

    print(f"\nFinished: {total_saved} looked up this run, {len(pending)} still to do.")
    if pending:
        print("Run the same command again later; VINs already looked up are skipped.")


if __name__ == "__main__":
    main()

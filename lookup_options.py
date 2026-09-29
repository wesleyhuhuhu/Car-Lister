"""
Look up the factory options for specific VINs and save them into
listings.json / listings.csv, replacing the `options` column for those rows.

This is deliberately separate from main.py: bimmer.work can rate-limit, so it
only runs for the VINs you name. A VIN that has been looked up is remembered
(its full build sheet is stored under "build_sheet" in the JSON) and is skipped
next time, and re-running main.py never overwrites it.

    python lookup_options.py WBS23HJ03VFX24999
    python lookup_options.py VIN1 VIN2 VIN3 --delay 15
    python lookup_options.py WBS23HJ03VFX24999 --force     # check it again

Each VIN must already be in the saved listings (run main.py first). Only BMW
is supported so far. Results are saved after every VIN, so stopping partway
loses nothing.
"""
import argparse
import sys
import time

from bmw_lookup import BmwSession
from listing_store import load_rows, save_all
from vin_router import route_listing


def options_as_list(build_sheet: dict) -> list[str]:
    """{"248": "Steering Wheel Heating"} -> ["248 Steering Wheel Heating"]"""
    return [f"{code} {description}".strip()
            for code, description in build_sheet.get("Options", {}).items()]


def main():
    parser = argparse.ArgumentParser(description="Look up factory options for specific VINs.")
    parser.add_argument("vins", nargs="+", help="VIN(s) to look up; each must already be in the saved listings")
    parser.add_argument("--out-prefix", default="listings")
    parser.add_argument("--force", action="store_true",
                         help="Re-check VINs that already have a saved lookup")
    parser.add_argument("--delay", type=float, default=5.0,
                         help="Seconds to wait between lookups (be gentle; the site can rate-limit)")
    parser.add_argument("--max-failures", type=int, default=3,
                         help="Stop after this many failed lookups in a row (likely rate-limited or blocked)")
    args = parser.parse_args()

    json_path = f"{args.out_prefix}.json"
    rows = load_rows(json_path)
    if not rows:
        sys.exit(f"No saved listings found in {json_path}. Run main.py first.")
    by_vin = {(r.get("vin") or "").upper(): r for r in rows if r.get("vin")}

    to_check = []
    seen = set()
    for raw in args.vins:
        vin = raw.strip().upper()
        if vin in seen:
            continue
        seen.add(vin)
        row = by_vin.get(vin)
        if row is None:
            print(f"{vin}: not in {json_path} -- run main.py first. Skipped.")
            continue
        make, _ = route_listing(title=row.get("title"), vin=vin)
        if make != "BMW":
            print(f"{vin}: no options lookup for {make or 'unknown make'} yet. Skipped.")
            continue
        if row.get("build_sheet") and not args.force:
            print(f"{vin}: already looked up ({len(row.get('options') or [])} options saved). "
                  "Skipped (use --force to check again).")
            continue
        to_check.append((vin, row))

    if not to_check:
        print("Nothing to look up.")
        return

    saved = failed = in_a_row = 0
    with BmwSession() as session:
        for i, (vin, row) in enumerate(to_check, start=1):
            if i > 1:
                time.sleep(args.delay)
            print(f"[{i}/{len(to_check)}] {vin}  {row.get('title', '')}")
            try:
                build_sheet = session.lookup(vin)
            except Exception as exc:
                failed += 1
                in_a_row += 1
                print(f"  failed: {exc.__class__.__name__}: {exc}")
                if in_a_row >= args.max_failures:
                    print(f"Stopping after {in_a_row} failures in a row (rate-limited or blocked?). "
                          "Wait a while, then run it again for the remaining VINs.")
                    break
                continue
            in_a_row = 0
            row["options"] = options_as_list(build_sheet)
            row["build_sheet"] = build_sheet
            save_all(rows, args.out_prefix)
            saved += 1
            print(f"  saved {len(row['options'])} options")

    print(f"Done: {saved} saved, {failed} failed, {len(to_check) - saved - failed} not attempted.")


if __name__ == "__main__":
    main()

"""
Fill in missing year / make / model / trim on rows already saved in
listings.json, using NHTSA. Separate from main.py on purpose: a search only
ever updates the listings it finds, and this script is the one place that goes
through everything on file (sold listings, VINs that failed to decode earlier,
rows from any earlier search).

    python fill_missing.py --dry-run      # show which rows are missing data
    python fill_missing.py                # decode them, save, update the database

A row counts as missing data when it has a VIN but no year, make or model.
Values already saved are never overwritten or blanked. A trim is filled in if
NHTSA has one, but a blank trim alone does not make a row "missing" (many
vehicles genuinely have none). Rows with no VIN can't be decoded and are skipped.
"""
import argparse
import sys

from db_push import db_configured, push_rows
from listing_store import load_rows, row_key, save_all
from vin_lookup import decode_all

SPEC_FIELDS = ("year", "make", "model", "trim")
REQUIRED = ("year", "make", "model")


def missing_rows(rows: list[dict]) -> list[dict]:
    return [r for r in rows
            if (r.get("vin") or "").strip() and not all(r.get(f) for f in REQUIRED)]


def main():
    parser = argparse.ArgumentParser(description="Fill in missing year/make/model/trim on saved rows.")
    parser.add_argument("--out-prefix", default="listings")
    parser.add_argument("--dry-run", action="store_true", help="List the rows that are missing data and exit")
    parser.add_argument("--batch-size", type=int, default=50, help="VINs per NHTSA request (max 50)")
    parser.add_argument("--no-db", action="store_true", help="Don't push the updated rows to the online database")
    args = parser.parse_args()

    json_path = f"{args.out_prefix}.json"
    rows = load_rows(json_path)
    if not rows:
        sys.exit(f"No saved listings found in {json_path}.")
    targets = missing_rows(rows)
    no_vin = sum(1 for r in rows if not (r.get("vin") or "").strip())
    print(f"{len(rows)} listings on file: {len(targets)} missing year/make/model"
          + (f", {no_vin} have no VIN (can't be decoded)." if no_vin else "."))
    if not targets:
        return
    if args.dry_run:
        for r in targets[:25]:
            print(f"  {r['vin']}  {r.get('title', '')[:60]}")
        if len(targets) > 25:
            print(f"  ... and {len(targets) - 25} more")
        return

    by_vin: dict[str, list[dict]] = {}
    for r in targets:
        by_vin.setdefault(r["vin"].strip().upper(), []).append(r)
    changed: dict[str, dict] = {}
    done = 0

    def apply(specs_by_vin: dict) -> None:
        nonlocal done
        for vin, specs in specs_by_vin.items():
            for row in by_vin.get(vin, []):
                for field in SPEC_FIELDS:
                    if specs.get(field) and not row.get(field):
                        row[field] = specs[field]
                        changed[row_key(row)] = row
            done += 1
        print(f"  decoded {done}/{len(by_vin)}", flush=True)
        save_all(rows, args.out_prefix)

    print(f"Decoding {len(by_vin)} VINs ({-(-len(by_vin) // args.batch_size)} NHTSA request(s))...")
    failed = decode_all(list(by_vin), batch_size=args.batch_size, on_batch=apply)

    still_missing = len(missing_rows(rows))
    print(f"Filled in {len(changed)} row(s). {still_missing} still missing data"
          + (f" ({sum(len(by_vin[v]) for v in failed if v in by_vin)} because NHTSA could not be reached; "
             "run again later)." if failed else " (NHTSA has no data for them)."))

    if changed and not args.no_db:
        if db_configured():
            print("Updating the online database...")
            if not push_rows(list(changed.values())):
                print("Database not updated; run db_sync_https.py later to catch up.")
        else:
            print("Online database not configured (SUPABASE_URL plus SUPABASE_SERVICE_KEY or SUPABASE_ANON_KEY): skipped.")


if __name__ == "__main__":
    main()

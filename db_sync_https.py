"""
Sync all of listings.json to Supabase over HTTPS (port 443), for networks that
block the database ports (5432/6543). main.py and the lookup scripts already
push their own changes automatically; use this to catch the database up after
a failed push, or to load an existing listings.json for the first time.

One-time setup:
  1. Supabase dashboard > SQL Editor > paste the contents of schema.sql > Run.
  2. Supabase > Project Settings > API: copy the Project URL and the
     secret / service_role key (NOT the anon/publishable key).
  3. Put both in .env next to this script (keep it git-ignored, the key is a password):
       SUPABASE_URL=https://xxxxxxxx.supabase.co
       SUPABASE_SERVICE_KEY=eyJ...

Usage:
  python db_sync_https.py            # sync listings.json
  python db_sync_https.py --dry-run
"""
import argparse
import sys

from db_push import db_configured, push_rows, to_payload
from db_sync import to_params
from listing_store import load_rows


def main():
    parser = argparse.ArgumentParser(description="Sync listings.json to Supabase over HTTPS.")
    parser.add_argument("--json", default="listings.json")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--batch-size", type=int, default=100)
    args = parser.parse_args()

    rows = load_rows(args.json)
    if not rows:
        sys.exit(f"No listings found in {args.json}.")
    with_lookup = sum(1 for r in rows if to_payload(to_params(r)).get("build_sheet"))
    print(f"{len(rows)} listings: {with_lookup} with options lookup, {len(rows) - with_lookup} without")
    if args.dry_run:
        return
    if not db_configured():
        sys.exit("Set SUPABASE_URL and SUPABASE_SERVICE_KEY, or SUPABASE_ANON_KEY to contribute (environment or .env). See the top of this file.")
    if not push_rows(rows, batch_size=args.batch_size):
        sys.exit(1)
    print("Synced.")


if __name__ == "__main__":
    main()

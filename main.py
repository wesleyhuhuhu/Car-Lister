"""
Scrape AutoTempest search results into listings.json / listings.csv.

  1. Search AutoTempest (autotempest_scraper.py): title, price, mileage,
     location, source site, link and VIN come straight off the search page.
  2. Decode the VINs with NHTSA's free vPIC API (50 per request) for year/make/model/trim.
     VINs already saved with specs reuse them instead of calling the API.
  3. Merge into any existing listings.json by VIN (a known VIN has its
     price/mileage/etc. refreshed; a new VIN is appended) and save JSON + CSV.

If the online database is configured (.env), the merged listings are then pushed
to it too (changed rows are updated); --no-db skips that.

Only the listings this search returns are decoded. To fill in missing specs on
rows already saved (sold listings, earlier failures), use fill_missing.py.

Factory options are NOT collected here, and rows you already looked up keep
their saved options through every re-scrape. To fetch options for specific
VINs, use lookup_options.py.

Usage:
    python main.py --make bmw --model m3 --zip 91748 --radius 500 --minyear 2021
"""
import argparse

from autotempest_scraper import build_search_url, scrape_search
from db_push import db_configured, push_rows
from listing_store import load_rows, merge_rows, row_key, save_all
from vin_lookup import decode_all

SPEC_FIELDS = ("year", "make", "model", "trim")


def fill_specs(
    listings,
    existing_by_key: dict[str, dict] | None = None,
    batch_size: int = 50,
    on_progress=None,
) -> dict:
    """Fill year/make/model/trim on each listing.

    - A VIN already saved with year/make/model is reused (no NHTSA call).
    - Everything else is decoded in batches of up to 50 VINs per request.
    - Values already known are never blanked: whatever a saved row has is
      carried over, and a VIN NHTSA can't decode (or can't be reached for) just
      keeps what it had.
    Calls `on_progress()` after every batch (used for incremental saving) and
    returns a stats dict."""
    existing_by_key = existing_by_key or {}
    stats = {"reused": 0, "decoded": 0, "no_specs": 0, "no_vin": 0, "not_decoded": 0}
    pending: dict[str, list] = {}          # vin -> listings that need it

    for listing in listings:
        existing = existing_by_key.get(
            row_key({"vin": listing.vin, "listing_url": listing.listing_url})
        )
        if existing:                        # carry over anything already known
            for field in SPEC_FIELDS:
                if not getattr(listing, field) and existing.get(field):
                    setattr(listing, field, existing[field])
        if existing and all(existing.get(f) for f in ("year", "make", "model")):
            stats["reused"] += 1
        elif not listing.vin:
            stats["no_vin"] += 1
        else:
            pending.setdefault(listing.vin.strip().upper(), []).append(listing)

    total = len(pending)
    done = 0
    if total:
        print(f"  {stats['reused']} VINs already have specs; decoding {total} VINs "
              f"({-(-total // batch_size)} NHTSA request(s))...")

    def apply(specs_by_vin: dict) -> None:
        nonlocal done
        for vin, specs in specs_by_vin.items():
            for listing in pending.get(vin, []):
                for field in SPEC_FIELDS:
                    if specs.get(field) and not getattr(listing, field):
                        setattr(listing, field, specs[field])
            found = any(specs.get(f) for f in SPEC_FIELDS)
            stats["decoded" if found else "no_specs"] += len(pending.get(vin, []))
            done += 1
        print(f"  decoded {done}/{total}", flush=True)
        if on_progress:
            on_progress()

    if total:
        failed = decode_all(list(pending), batch_size=batch_size, on_batch=apply)
        stats["not_decoded"] = sum(len(pending[v]) for v in failed if v in pending)
        if stats["not_decoded"]:
            print(f"  {stats['not_decoded']} listing(s) could not be decoded this time "
                  "(NHTSA unreachable or rate limiting). Their existing data was kept; "
                  "run again later to fill them in.")
    if on_progress:
        on_progress()
    return stats


def main():
    parser = argparse.ArgumentParser(description="Scrape AutoTempest into listings.json / listings.csv.")
    parser.add_argument("--make", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--trim", default=None, help='Trim keyword, e.g. "Competition" (sent as trim_kw)')
    parser.add_argument("--zip", required=True)
    parser.add_argument("--radius", type=str, default="50",
                         help="Miles (25/50/100/300/500/1000), or 'state', 'country' (nationwide), or 'any' (anywhere)")
    parser.add_argument("--minyear", type=int)
    parser.add_argument("--maxyear", type=int)
    parser.add_argument("--minprice", type=int)
    parser.add_argument("--maxprice", type=int)
    parser.add_argument("--max-listings", type=int, default=None,
                         help="Cap on listings scraped; omit for no cap (grab everything available)")
    parser.add_argument("--click-more-rounds", type=int, default=15,
                         help="How many times to click each source's 'More Results' button to load additional pages")
    parser.add_argument("--decode-batch-size", type=int, default=50,
                         help="VINs per NHTSA request (max 50)")
    parser.add_argument("--headless", action="store_true", default=True)
    parser.add_argument("--out-prefix", default="listings")
    parser.add_argument("--no-db", action="store_true",
                         help="Don't push the listings to the online database (see db_push.py)")
    parser.add_argument("--no-merge", action="store_true",
                         help="Ignore any existing saved listings (they are overwritten, saved lookups included)")
    args = parser.parse_args()

    search_url = build_search_url(
        make=args.make, model=args.model, zip=args.zip, radius=args.radius, trim_kw=args.trim,
        minyear=args.minyear, maxyear=args.maxyear,
        minprice=args.minprice, maxprice=args.maxprice,
    )
    print(f"Searching: {search_url}")
    listings = scrape_search(
        search_url,
        max_listings=args.max_listings,
        headless=args.headless,
        click_more_rounds=args.click_more_rounds,
    )
    print(f"Found {len(listings)} listings. Decoding VINs (year/make/model/trim)...")

    json_path = f"{args.out_prefix}.json"
    existing_rows = [] if args.no_merge else load_rows(json_path)
    existing_by_key = {row_key(r): r for r in existing_rows}
    if existing_rows:
        print(f"Loaded {len(existing_rows)} previously saved listings.")

    def save_progress():
        save_all(merge_rows(existing_rows, listings), args.out_prefix)

    stats = fill_specs(
        listings,
        existing_by_key=existing_by_key,
        batch_size=args.decode_batch_size,
        on_progress=save_progress,
    )
    print(f"VIN decode summary: {stats}")

    merged_rows = merge_rows(existing_rows, listings)
    save_all(merged_rows, args.out_prefix)
    print(f"Saved {args.out_prefix}.csv and {json_path} ({len(merged_rows)} total listings on file)")

    if args.no_db:
        pass
    elif db_configured():
        print("Updating the online database...")
        if not push_rows(merged_rows):
            print("Database not updated; the local files are saved. Run db_sync_https.py later to catch up.")
    else:
        print("Online database not configured (SUPABASE_URL plus SUPABASE_SERVICE_KEY or SUPABASE_ANON_KEY): skipped.")


if __name__ == "__main__":
    main()

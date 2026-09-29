"""
Scrape AutoTempest search results into listings.json / listings.csv.

  1. Search AutoTempest (autotempest_scraper.py): title, price, mileage,
     location, source site, link and VIN come straight off the search page.
  2. Decode each VIN with NHTSA's free vPIC API for year/make/model/trim.
     VINs already saved with specs reuse them instead of calling the API.
  3. Merge into any existing listings.json by VIN (a known VIN has its
     price/mileage/etc. refreshed; a new VIN is appended) and save JSON + CSV.

If the online database is configured (.env), the merged listings are then pushed
to it too (changed rows are updated); --no-db skips that.

Factory options are NOT collected here, and rows you already looked up keep
their saved options through every re-scrape. To fetch options for specific
VINs, use lookup_options.py.

Usage:
    python main.py --make bmw --model m3 --zip 91748 --radius 500 --minyear 2021
"""
import argparse
import concurrent.futures
import threading

from autotempest_scraper import build_search_url, scrape_search
from db_push import db_configured, push_rows
from listing_store import load_rows, merge_rows, row_key, save_all
from vin_lookup import decode_vin_nhtsa

SPEC_FIELDS = ("year", "make", "model", "trim")


def fill_specs(
    listings,
    existing_by_key: dict[str, dict] | None = None,
    max_workers: int = 8,
    save_every: int = 25,
    on_progress=None,
) -> dict:
    """Fill year/make/model/trim on each listing, concurrently. A VIN that is
    already saved with specs reuses them instead of calling NHTSA again.
    Calls `on_progress()` every `save_every` completions (used for incremental
    saving) and returns a stats dict."""
    existing_by_key = existing_by_key or {}
    stats = {"reused": 0, "decoded": 0, "no_specs": 0, "no_vin": 0}
    stats_lock = threading.Lock()
    completed = 0
    total = len(listings)

    def process(listing):
        existing = existing_by_key.get(
            row_key({"vin": listing.vin, "listing_url": listing.listing_url})
        )
        if existing and all(existing.get(f) for f in ("year", "make", "model")):
            specs, status = existing, "reused"
        elif not listing.vin:
            specs, status = {}, "no_vin"
        else:
            try:
                specs = decode_vin_nhtsa(listing.vin)
            except Exception:
                specs = {}
            # decode_vin_nhtsa returns a dict of Nones for a VIN NHTSA can't
            # decode, so check for real values rather than a non-empty dict.
            status = "decoded" if any(specs.get(f) for f in SPEC_FIELDS) else "no_specs"
        for field in SPEC_FIELDS:
            setattr(listing, field, getattr(listing, field) or specs.get(field))
        with stats_lock:
            stats[status] += 1
        return listing, status

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(process, l) for l in listings]
        for future in concurrent.futures.as_completed(futures):
            listing, status = future.result()
            completed += 1
            print(f"  [{completed}/{total}] {status:9s} {listing.title[:60]}")
            if on_progress and (completed % save_every == 0 or completed == total):
                on_progress()

    return stats


def main():
    parser = argparse.ArgumentParser(description="Scrape AutoTempest into listings.json / listings.csv.")
    parser.add_argument("--make", required=True)
    parser.add_argument("--model", required=True)
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
    parser.add_argument("--max-workers", type=int, default=8,
                         help="How many NHTSA VIN decodes to run concurrently")
    parser.add_argument("--save-every", type=int, default=25,
                         help="Re-save the output files after this many VIN decodes finish")
    parser.add_argument("--headless", action="store_true", default=True)
    parser.add_argument("--out-prefix", default="listings")
    parser.add_argument("--no-db", action="store_true",
                         help="Don't push the listings to the online database (see db_push.py)")
    parser.add_argument("--no-merge", action="store_true",
                         help="Ignore any existing saved listings (they are overwritten, saved lookups included)")
    args = parser.parse_args()

    search_url = build_search_url(
        make=args.make, model=args.model, zip=args.zip, radius=args.radius,
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
        max_workers=args.max_workers,
        save_every=args.save_every,
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
        print("Online database not configured (SUPABASE_URL / SUPABASE_SERVICE_KEY): skipped.")


if __name__ == "__main__":
    main()

"""
End-to-end pipeline:
  1. Search AutoTempest (autotempest_scraper.py)
  2. For each listing, visit its destination page and extract factory
     options (VIN usually already comes from the search page itself)
  3. Filter listings by option keywords
  4. Merge into any existing saved listings.json by VIN (existing VINs get
     updated in place; new ones get appended) and save CSV/JSON

Usage:
    python main.py --make toyota --model camry --zip 90001 --radius 50 \
        --require "sunroof,leather"
"""
import argparse
import csv
import json
import os
import time

from autotempest_scraper import build_search_url, scrape_search
from vin_lookup import enrich_listing

CSV_FIELDS = ["title", "price", "mileage", "year", "make", "model", "trim",
              "vin", "options", "source_site", "location", "listing_url"]


def enrich_all(listings, delay: float = 1.0):
    """Enrich each listing in place with options and NHTSA specs (VIN is
    reused if the search page already gave us one).
    `delay` throttles requests to the destination sites -- be a good citizen."""
    for listing in listings:
        if not listing.listing_url:
            continue
        data = enrich_listing(listing.listing_url, known_vin=listing.vin)
        listing.vin = data["vin"]
        listing.options = data["options"]
        specs = data.get("specs") or {}
        listing.year = listing.year or specs.get("year")
        listing.make = listing.make or specs.get("make")
        listing.model = listing.model or specs.get("model")
        listing.trim = listing.trim or specs.get("trim")
        time.sleep(delay)
    return listings


def filter_by_options(listings, required_keywords: list[str]):
    """Keep only listings whose extracted options mention ALL of the given
    keywords (case-insensitive substring match)."""
    if not required_keywords:
        return listings
    keywords = [k.strip().lower() for k in required_keywords if k.strip()]
    filtered = []
    for listing in listings:
        options_blob = " ".join(listing.options).lower()
        if all(kw in options_blob for kw in keywords):
            filtered.append(listing)
    return filtered


def _row_key(row: dict) -> str:
    """VIN when we have one (the normal case); fall back to the listing URL
    for the rare listing with no VIN, so it still merges consistently."""
    vin = (row.get("vin") or "").strip()
    return vin if vin else f"url:{row.get('listing_url')}"


def load_existing_rows(json_path: str) -> list[dict]:
    if not os.path.exists(json_path):
        return []
    with open(json_path, encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return []


def merge_rows(existing_rows: list[dict], new_listings) -> list[dict]:
    """Merge freshly scraped listings into the existing saved set, keyed by
    VIN: a VIN already on file gets its row updated in place (fresher price,
    mileage, options, etc.); a new VIN gets appended. Order is preserved --
    existing rows keep their position, new ones go at the end."""
    by_key: dict[str, dict] = {}
    order: list[str] = []
    for row in existing_rows:
        k = _row_key(row)
        by_key[k] = row
        order.append(k)

    for listing in new_listings:
        row = listing.to_dict()
        k = _row_key(row)
        if k in by_key:
            by_key[k].update(row)
        else:
            by_key[k] = row
            order.append(k)

    return [by_key[k] for k in order]


def save_csv(rows: list[dict], path: str):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            out = dict(row)
            options = out.get("options") or []
            if isinstance(options, list):
                out["options"] = "; ".join(options)
            writer.writerow({k: out.get(k) for k in CSV_FIELDS})


def save_json(rows: list[dict], path: str):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2)


def main():
    parser = argparse.ArgumentParser(description="Search AutoTempest and enrich with factory options.")
    parser.add_argument("--make", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--zip", required=True)
    parser.add_argument("--radius", type=int, default=50)
    parser.add_argument("--minyear", type=int)
    parser.add_argument("--maxyear", type=int)
    parser.add_argument("--minprice", type=int)
    parser.add_argument("--maxprice", type=int)
    parser.add_argument("--max-listings", type=int, default=None,
                         help="Cap on listings scraped; omit for no cap (grab everything available)")
    parser.add_argument("--click-more-rounds", type=int, default=15,
                         help="How many times to click each source's 'More Results' button to load additional pages")
    parser.add_argument("--require", type=str, default="",
                         help="Comma-separated option keywords that must all be present")
    parser.add_argument("--delay", type=float, default=1.0,
                         help="Seconds to wait between requests to destination sites")
    parser.add_argument("--headless", action="store_true", default=True)
    parser.add_argument("--out-prefix", default="listings")
    parser.add_argument("--no-merge", action="store_true",
                         help="Overwrite the output files instead of merging with any existing saved listings")
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
    print(f"Found {len(listings)} listings. Enriching with VIN + options...")

    enrich_all(listings, delay=args.delay)

    required = args.require.split(",") if args.require else []
    filtered = filter_by_options(listings, required)
    print(f"{len(filtered)} listings match required options: {required or 'none'}")

    json_path = f"{args.out_prefix}.json"
    if args.no_merge:
        merged_rows = [l.to_dict() for l in filtered]
    else:
        existing_rows = load_existing_rows(json_path)
        merged_rows = merge_rows(existing_rows, filtered)
        print(f"Merged with {len(existing_rows)} previously saved listings -> {len(merged_rows)} total")

    save_csv(merged_rows, f"{args.out_prefix}.csv")
    save_json(merged_rows, json_path)
    print(f"Saved {args.out_prefix}.csv and {json_path}")


if __name__ == "__main__":
    main()

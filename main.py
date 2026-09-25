"""
End-to-end pipeline:
  1. Search AutoTempest (autotempest_scraper.py)
  2. For each listing, visit its destination page and extract VIN + any
     published factory options (vin_lookup.py)
  3. Filter listings by option keywords
  4. Save results to CSV/JSON

Usage:
    python main.py --make toyota --model camry --zip 90001 --rad 50 \
        --max-listings 25 --require "sunroof,leather"
"""
import argparse
import csv
import json
import time

from autotempest_scraper import build_search_url, scrape_search
from vin_lookup import enrich_listing


def enrich_all(listings, delay: float = 1.0):
    """Enrich each listing in place with VIN, options, and NHTSA specs.
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


def save_csv(listings, path: str):
    fields = ["title", "price", "mileage", "year", "make", "model", "trim",
              "vin", "options", "source_site", "location", "listing_url"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for listing in listings:
            row = listing.to_dict()
            row["options"] = "; ".join(row["options"])
            writer.writerow({k: row.get(k) for k in fields})


def save_json(listings, path: str):
    with open(path, "w", encoding="utf-8") as f:
        json.dump([l.to_dict() for l in listings], f, indent=2)


def main():
    parser = argparse.ArgumentParser(description="Search AutoTempest and enrich with factory options.")
    parser.add_argument("--make", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--zip", required=True)
    parser.add_argument("--radius", type=int, default=100)
    parser.add_argument("--minyear", type=int)
    parser.add_argument("--maxyear", type=int)
    parser.add_argument("--minprice", type=int)
    parser.add_argument("--maxprice", type=int)
    parser.add_argument("--max-listings", type=int, default=20)
    parser.add_argument("--require", type=str, default="",
                         help="Comma-separated option keywords that must all be present")
    parser.add_argument("--delay", type=float, default=1.0,
                         help="Seconds to wait between requests to destination sites")
    parser.add_argument("--headless", action="store_true", default=True)
    parser.add_argument("--out-prefix", default="listings")
    args = parser.parse_args()

    search_url = build_search_url(
        make=args.make, model=args.model, zip=args.zip, radius=args.radius,
        minyear=args.minyear, maxyear=args.maxyear,
        minprice=args.minprice, maxprice=args.maxprice,
    )
    print(f"Searching: {search_url}")
    listings = scrape_search(search_url, max_listings=args.max_listings, headless=args.headless)
    print(f"Found {len(listings)} listings. Enriching with VIN + options...")

    enrich_all(listings, delay=args.delay)

    required = args.require.split(",") if args.require else []
    filtered = filter_by_options(listings, required)
    print(f"{len(filtered)} listings match required options: {required or 'none'}")

    save_csv(filtered, f"{args.out_prefix}.csv")
    save_json(filtered, f"{args.out_prefix}.json")
    print(f"Saved {args.out_prefix}.csv and {args.out_prefix}.json")


if __name__ == "__main__":
    main()

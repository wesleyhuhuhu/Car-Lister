"""
Bring the shared database's listings into listings.json / listings.csv, so the local scripts
(lookup_matching.py, lookup_options.py, ...) can work on everything other people have added.

    python db_pull.py               # merge the database into listings.json / .csv
    python db_pull.py --dry-run     # just say what would change

Needs SUPABASE_URL plus SUPABASE_ANON_KEY (or SUPABASE_SERVICE_KEY) in the environment or .env.
Reads the public_listings view, which anyone with the anon key may read.

Merging, by VIN (or link for the rare listing without one):
  * a listing only in the database is added;
  * for a listing in both, the database's scraped facts (title, price, mileage, location, ...) win, and
    year/make/model/trim are filled in where the local row has none;
  * looked-up options: the database's build sheet wins when it has one; otherwise the local one is kept
    (a lookup you did that never reached the database is not lost);
  * a listing only in the local file is kept.
"""
import argparse
import sys

import requests

try:                                   # company networks that inspect HTTPS
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

from db_push import read_env
from listing_store import load_rows, row_key, save_all

PAGE = 1000
COLUMNS = ("listing_key,vin,title,year,make,model,trim,price,price_text,mileage,mileage_text,"
           "source_site,location,listing_url,image_url,options,build_sheet")


def fetch_all(base: str, key: str) -> list[dict]:
    headers = {"apikey": key}
    if key.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {key}"
    rows: list[dict] = []
    while True:
        resp = requests.get(f"{base.rstrip('/')}/rest/v1/public_listings",
                            params={"select": COLUMNS, "order": "listing_key"},
                            headers={**headers, "Range": f"{len(rows)}-{len(rows) + PAGE - 1}"}, timeout=120)
        if resp.status_code >= 300:
            sys.exit(f"Could not read the database ({resp.status_code}): {resp.text[:300]}")
        batch = resp.json()
        rows.extend(batch)
        print(f"  read {len(rows)} listings...", flush=True)
        if len(batch) < PAGE:
            return rows


def to_local(db: dict) -> dict:
    """A public_listings row -> the listings.json row shape (price/mileage as the original text)."""
    def text_or_number(text, number, fmt):
        return text or (fmt.format(number) if number is not None else None)
    return {
        "title": db.get("title"),
        "price": text_or_number(db.get("price_text"), db.get("price"), "${:,}"),
        "mileage": text_or_number(db.get("mileage_text"), db.get("mileage"), "{:,} mi."),
        "source_site": db.get("source_site"),
        "location": db.get("location"),
        "listing_url": db.get("listing_url"),
        "image_url": db.get("image_url"),
        "vin": db.get("vin"),
        "year": str(db["year"]) if db.get("year") is not None else None,
        "make": db.get("make"),
        "model": db.get("model"),
        "trim": db.get("trim"),
        "options": list(db.get("options") or []) if db.get("build_sheet") else [],
        "build_sheet": db.get("build_sheet"),
    }


def merge(local_rows: list[dict], db_rows: list[dict]) -> tuple[list[dict], dict]:
    by_key = {row_key(r): r for r in local_rows}
    order = [row_key(r) for r in local_rows]
    stats = {"added": 0, "updated": 0, "options_from_db": 0, "kept_local_options": 0}
    for db in db_rows:
        new = to_local(db)
        k = row_key(new)
        old = by_key.get(k)
        if old is None:
            by_key[k] = new
            order.append(k)
            stats["added"] += 1
            continue
        stats["updated"] += 1
        for field in ("title", "price", "mileage", "source_site", "location", "listing_url", "image_url", "vin"):
            if new.get(field) not in (None, ""):
                old[field] = new[field]
        for field in ("year", "make", "model", "trim"):
            if old.get(field) in (None, "") and new.get(field) not in (None, ""):
                old[field] = new[field]
        if new.get("build_sheet"):
            if old.get("build_sheet") != new["build_sheet"]:
                stats["options_from_db"] += 1
            old["build_sheet"], old["options"] = new["build_sheet"], new["options"]
        elif old.get("build_sheet"):
            stats["kept_local_options"] += 1
    return [by_key[k] for k in order], stats


def main():
    parser = argparse.ArgumentParser(description="Merge the shared database into listings.json / listings.csv.")
    parser.add_argument("--out-prefix", default="listings")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change; write nothing")
    args = parser.parse_args()

    base = read_env("SUPABASE_URL")
    key = read_env("SUPABASE_ANON_KEY") or read_env("SUPABASE_SERVICE_KEY")
    if not base or not key:
        sys.exit("Set SUPABASE_URL and SUPABASE_ANON_KEY (environment variable or .env file).")

    local_rows = load_rows(f"{args.out_prefix}.json")
    print(f"{len(local_rows)} listings saved locally. Reading the database...")
    db_rows = fetch_all(base, key)
    merged, stats = merge(local_rows, db_rows)
    print(f"Database: {len(db_rows)} listings. {stats['added']} new here, {stats['updated']} refreshed, "
          f"{stats['options_from_db']} got options from the database, "
          f"{stats['kept_local_options']} kept a local options lookup the database doesn't have yet.")
    if args.dry_run:
        print("Dry run: nothing written.")
        return
    save_all(merged, args.out_prefix)
    print(f"Saved {args.out_prefix}.json and {args.out_prefix}.csv ({len(merged)} listings).")
    if stats["kept_local_options"]:
        print("To send those local lookups to the database, run: python db_sync_https.py")


if __name__ == "__main__":
    main()

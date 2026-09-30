"""
Push listings.json into an online PostgreSQL database (Supabase, Neon, ...).

  python db_sync.py --init        # create the table (runs schema.sql) and sync
  python db_sync.py               # sync listings.json
  python db_sync.py --dry-run     # show what would be sent, no connection

The connection string comes from the DATABASE_URL environment variable or a
.env file next to this script:
    DATABASE_URL=postgresql://user:password@host:5432/dbname

listings.json stays the source of truth. Each run upserts every row by VIN:
scraped fields (price, mileage, ...) are refreshed, and a saved factory-options
lookup is never erased by a row that has none.
"""
import argparse
import os
import re
import sys
from datetime import datetime, timezone

from listing_store import load_rows, row_key

UPSERT_SQL = """
insert into listings (
    listing_key, vin, title, year, make, model, trim,
    price, price_text, mileage, mileage_text,
    source_site, location, listing_url, image_url,
    options, option_codes, build_sheet, options_checked_at, updated_at
) values (
    %(listing_key)s, %(vin)s, %(title)s, %(year)s, %(make)s, %(model)s, %(trim)s,
    %(price)s, %(price_text)s, %(mileage)s, %(mileage_text)s,
    %(source_site)s, %(location)s, %(listing_url)s, %(image_url)s,
    %(options)s::text[], %(option_codes)s::text[], %(build_sheet)s::jsonb,
    case when %(build_sheet)s::jsonb is not null then now() end, now()
)
on conflict (listing_key) do update set
    vin          = excluded.vin,
    title        = excluded.title,
    year         = coalesce(excluded.year, listings.year),
    make         = coalesce(excluded.make, listings.make),
    model        = coalesce(excluded.model, listings.model),
    trim         = coalesce(excluded.trim, listings.trim),
    price        = coalesce(excluded.price, listings.price),
    price_text   = coalesce(excluded.price_text, listings.price_text),
    mileage      = coalesce(excluded.mileage, listings.mileage),
    mileage_text = coalesce(excluded.mileage_text, listings.mileage_text),
    source_site  = coalesce(excluded.source_site, listings.source_site),
    location     = coalesce(excluded.location, listings.location),
    listing_url  = excluded.listing_url,
    image_url    = coalesce(excluded.image_url, listings.image_url),
    options      = case when excluded.build_sheet is not null then excluded.options      else listings.options      end,
    option_codes = case when excluded.build_sheet is not null then excluded.option_codes else listings.option_codes end,
    build_sheet  = coalesce(excluded.build_sheet, listings.build_sheet),
    options_checked_at = case when excluded.build_sheet is not null
                              then coalesce(listings.options_checked_at, excluded.options_checked_at)
                              else listings.options_checked_at end,
    updated_at   = now()
"""


def parse_int(value):
    """'$45,995' -> 45995, '12,345 mi' -> 12345, anything without digits -> None."""
    if value is None:
        return None
    digits = re.sub(r"[^\d]", "", str(value).split(".")[0])
    return int(digits) if digits else None


def to_params(row: dict) -> dict:
    """Turn one listings.json row into the parameters for UPSERT_SQL."""
    import json
    sheet = row.get("build_sheet")
    options = row.get("options") if sheet else None
    codes = list((sheet or {}).get("Options", {}).keys()) if sheet else []
    return {
        "listing_key": row_key(row),
        "vin": (row.get("vin") or "").strip() or None,
        "title": row.get("title") or "",
        "year": parse_int(row.get("year")),
        "make": row.get("make"),
        "model": row.get("model"),
        "trim": row.get("trim"),
        "price": parse_int(row.get("price")),
        "price_text": row.get("price"),
        "mileage": parse_int(row.get("mileage")),
        "mileage_text": row.get("mileage"),
        "source_site": row.get("source_site"),
        "location": row.get("location"),
        "listing_url": row.get("listing_url"),
        "image_url": row.get("image_url"),
        "options": [str(o) for o in (options or [])],
        "option_codes": [str(c) for c in codes],
        "build_sheet": json.dumps(sheet) if sheet else None,
    }


def load_database_url() -> str | None:
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("DATABASE_URL="):
                    return line.split("=", 1)[1].strip().strip("'\"")
    return None


def main():
    parser = argparse.ArgumentParser(description="Sync listings.json to an online PostgreSQL database.")
    parser.add_argument("--json", default="listings.json")
    parser.add_argument("--init", action="store_true", help="Create the table first (runs schema.sql)")
    parser.add_argument("--dry-run", action="store_true", help="Show a summary without connecting")
    parser.add_argument("--batch-size", type=int, default=50)
    args = parser.parse_args()

    rows = load_rows(args.json)
    if not rows:
        sys.exit(f"No listings found in {args.json}.")
    params = [to_params(r) for r in rows]
    with_options = sum(1 for p in params if p["build_sheet"])
    print(f"{len(params)} listings in {args.json}, {with_options} with looked-up factory options.")

    if args.dry_run:
        for p in params[:3]:
            print({k: p[k] for k in ("listing_key", "year", "trim", "price", "mileage", "option_codes")})
        return

    url = load_database_url()
    if not url:
        sys.exit("Set DATABASE_URL (environment variable or .env file). See the README.")
    try:
        import psycopg
    except ImportError:
        sys.exit("Install the driver first:  pip install \"psycopg[binary]\"")

    print("Connecting...", flush=True)
    with psycopg.connect(url, connect_timeout=20) as conn:
        print("Connected.", flush=True)
        with conn.cursor() as cur:
            if args.init:
                schema_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")
                with open(schema_path, encoding="utf-8") as f:
                    cur.execute(f.read())
                conn.commit()  # table is visible right away
                print("Table ready.", flush=True)
            for i in range(0, len(params), args.batch_size):
                batch = params[i:i + args.batch_size]
                for p in batch:
                    cur.execute(UPSERT_SQL, p)
                conn.commit()  # rows show up in Supabase after every batch
                print(f"  {min(i + args.batch_size, len(params))}/{len(params)} synced", flush=True)
            cur.execute("select count(*), count(build_sheet) from listings")
            total, looked_up = cur.fetchone()
    print(f"Synced. Database now holds {total} listings, {looked_up} with factory options.")


if __name__ == "__main__":
    main()

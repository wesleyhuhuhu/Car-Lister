"""
Reading and writing the saved listings (listings.json / listings.csv).

Shared by main.py (AutoTempest scrape) and lookup_options.py (per-VIN factory
options), so both agree on the file format and on how rows are matched (by VIN).
"""
import csv
import json
import os

CSV_FIELDS = ["title", "price", "mileage", "year", "make", "model", "trim",
              "vin", "options", "source_site", "location", "listing_url"]

# Filled in only by lookup_options.py. A fresh AutoTempest scrape must never
# overwrite these, otherwise re-running main.py would wipe saved lookups.
LOOKUP_FIELDS = ("options", "build_sheet")


def row_key(row: dict) -> str:
    """VIN when we have one (the normal case); fall back to the listing URL
    for the rare listing with no VIN, so it still merges consistently."""
    vin = (row.get("vin") or "").strip()
    return vin if vin else f"url:{row.get('listing_url')}"


def load_rows(json_path: str) -> list[dict]:
    if not os.path.exists(json_path):
        return []
    with open(json_path, encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return []


def merge_rows(existing_rows: list[dict], new_listings) -> list[dict]:
    """Merge freshly scraped listings into the saved rows, keyed by VIN.

    A VIN already on file has its row refreshed (price, mileage, location...)
    except for LOOKUP_FIELDS, which are kept exactly as saved. A new VIN is
    appended. Existing rows keep their position; new ones go at the end."""
    by_key: dict[str, dict] = {}
    order: list[str] = []
    for row in existing_rows:
        k = row_key(row)
        by_key[k] = row
        order.append(k)

    for listing in new_listings:
        row = listing.to_dict()
        k = row_key(row)
        if k in by_key:
            by_key[k].update({f: v for f, v in row.items() if f not in LOOKUP_FIELDS})
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
        json.dump(rows, f, indent=2, ensure_ascii=False)


def save_all(rows: list[dict], out_prefix: str):
    """Write <prefix>.json first, then <prefix>.csv. If the CSV is open in
    Excel (Windows locks it), warn instead of crashing -- the JSON is saved."""
    save_json(rows, f"{out_prefix}.json")
    try:
        save_csv(rows, f"{out_prefix}.csv")
    except PermissionError:
        print(f"  (could not write {out_prefix}.csv -- is it open in Excel? The JSON was saved.)")

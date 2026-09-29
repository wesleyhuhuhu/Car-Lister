"""
Push listing rows to the online Supabase database over HTTPS.

Used automatically by main.py and the lookup scripts (they save the local
listings.json first, then call push_rows). Every push is an upsert by VIN, so a
changed price/mileage updates the existing database row, and a row with no
saved options lookup never erases one already in the database.

Never raises: if the database can't be reached, a warning is printed and the
script carries on (the local files are already saved). Run db_sync_https.py
later to catch the database up.

Needs SUPABASE_URL and SUPABASE_SERVICE_KEY (environment or .env file). With
neither set, database pushing is simply off.
"""
import json
import os

import requests

# On a company network that inspects HTTPS, trust the Windows certificate store.
# `pip install truststore` turns this on; it is optional.
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

from db_sync import to_params

LOOKUP_COLUMNS = ("options", "option_codes", "build_sheet")


def read_env(name: str) -> str | None:
    if os.environ.get(name):
        return os.environ[name]
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith(name + "="):
                    return line.split("=", 1)[1].strip().strip("'\"")
    return None


def db_configured() -> bool:
    return bool(read_env("SUPABASE_URL") and read_env("SUPABASE_SERVICE_KEY"))


def to_payload(params: dict) -> dict:
    """UPSERT parameters -> a JSON row for the REST API. A row with no looked-up
    options leaves the option columns out entirely, so a saved lookup already in
    the database is never overwritten."""
    row = dict(params)
    if row["build_sheet"]:
        row["build_sheet"] = json.loads(row["build_sheet"])
    else:
        for col in LOOKUP_COLUMNS:
            row.pop(col)
    return row


def _headers(key: str) -> dict:
    h = {"apikey": key, "Content-Type": "application/json",
         "Prefer": "resolution=merge-duplicates,return=minimal"}
    if key.startswith("eyJ"):          # legacy JWT-style keys also go in Authorization
        h["Authorization"] = f"Bearer {key}"
    return h


def push_rows(rows: list[dict], batch_size: int = 100, quiet: bool = False) -> bool:
    """Upsert listing rows (as saved in listings.json) into the database.
    Returns True on success, False (after printing a warning) on any failure."""
    if not rows:
        return True
    base, key = read_env("SUPABASE_URL"), read_env("SUPABASE_SERVICE_KEY")
    if not base or not key:
        print("  database not configured (SUPABASE_URL / SUPABASE_SERVICE_KEY): skipped")
        return False
    endpoint = base.rstrip("/") + "/rest/v1/listings?on_conflict=listing_key"

    payloads = [to_payload(to_params(r)) for r in rows]
    # PostgREST needs every row in one request to have the same columns, so rows
    # with and without a saved lookup go in separate groups.
    groups = [
        [p for p in payloads if "build_sheet" in p],
        [p for p in payloads if "build_sheet" not in p],
    ]
    sent = 0
    try:
        for items in groups:
            for i in range(0, len(items), batch_size):
                batch = items[i:i + batch_size]
                resp = requests.post(endpoint, json=batch, headers=_headers(key), timeout=30)
                if resp.status_code >= 300:
                    print(f"  database push failed ({resp.status_code}): {resp.text[:300]}")
                    return False
                sent += len(batch)
                if not quiet and len(payloads) > batch_size:
                    print(f"  database: {sent}/{len(payloads)} synced", flush=True)
    except requests.RequestException as exc:
        print(f"  database push failed: {exc.__class__.__name__}: {exc}")
        return False
    if not quiet:
        print(f"  database: {len(payloads)} listing(s) synced")
    return True

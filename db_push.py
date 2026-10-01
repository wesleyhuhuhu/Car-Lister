"""
Push listing rows to the online Supabase database over HTTPS.

Used automatically by main.py and the lookup scripts (they save the local
listings.json first, then call push_rows). Every push is an upsert by VIN, so a
changed price/mileage updates the existing database row, and a row with no
saved options lookup never erases one already in the database.

Never raises: if the database can't be reached, a warning is printed and the
script carries on (the local files are already saved). Run db_sync_https.py
later to catch the database up.

Two ways to be allowed to push (environment or .env file):
  * owner:        SUPABASE_URL + SUPABASE_SERVICE_KEY  -> direct upsert into the table
  * contributor:  SUPABASE_URL + SUPABASE_ANON_KEY     -> the submit_listings database
                  function (adds new listings, refreshes price/mileage of known ones,
                  never touches options; options found by lookups go through submit_options)
With neither set, database pushing is simply off.
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
    return bool(read_env("SUPABASE_URL") and (read_env("SUPABASE_SERVICE_KEY") or read_env("SUPABASE_ANON_KEY")))


def is_owner() -> bool:
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
    # Leave out empty fields entirely so an upsert never blanks a value the
    # database already has (e.g. a year/make/model/trim from an earlier sync).
    return {k: v for k, v in row.items() if v is not None}


def _headers(key: str) -> dict:
    h = {"apikey": key, "Content-Type": "application/json",
         "Prefer": "resolution=merge-duplicates,return=minimal"}
    if key.startswith("eyJ"):          # legacy JWT-style keys also go in Authorization
        h["Authorization"] = f"Bearer {key}"
    return h


CONTRIBUTOR_BATCH = 200           # listings per submit_listings call (the database accepts up to 500)
LISTING_FIELDS = ("title", "vin", "year", "make", "model", "trim", "price", "price_text", "mileage",
                  "mileage_text", "source_site", "location", "listing_url", "image_url")


def _rpc(base: str, key: str, name: str, args: dict):
    resp = requests.post(f"{base.rstrip('/')}/rest/v1/rpc/{name}", json=args, headers=_headers(key), timeout=60)
    if resp.status_code >= 300:
        raise RuntimeError(f"{name} failed ({resp.status_code}): {resp.text[:300]}")
    return resp.json() if resp.text else None


def push_rows_as_contributor(rows: list[dict], send_options: bool = False, quiet: bool = False) -> bool:
    """Push with the public anon key through submit_listings (and, when send_options is
    set, submit_options for rows that carry a looked-up build sheet)."""
    base, key = read_env("SUPABASE_URL"), read_env("SUPABASE_ANON_KEY")
    params = [to_params(r) for r in rows]
    payloads = [{k: p[k] for k in LISTING_FIELDS if p.get(k) not in (None, "")} for p in params]
    totals = {"inserted": 0, "updated": 0, "skipped": 0}
    try:
        for i in range(0, len(payloads), CONTRIBUTOR_BATCH):
            chunk = payloads[i:i + CONTRIBUTOR_BATCH]
            res = _rpc(base, key, "submit_listings", {"p_rows": chunk})
            res = res[0] if isinstance(res, list) and res else (res or {})
            for k in totals:
                totals[k] += int(res.get(k, 0) or 0)
            if not quiet and len(payloads) > CONTRIBUTOR_BATCH:
                print(f"  database: {min(i + CONTRIBUTOR_BATCH, len(payloads))}/{len(payloads)} sent", flush=True)
        if send_options:
            for p in params:
                if p["build_sheet"] and p["vin"]:
                    _rpc(base, key, "submit_options", {"p_vin": p["vin"], "p_build_sheet": json.loads(p["build_sheet"])})
    except (requests.RequestException, RuntimeError) as exc:
        print(f"  database push failed: {exc.__class__.__name__}: {exc}")
        return False
    if not quiet:
        print(f"  database: {totals['inserted']} new, {totals['updated']} refreshed, {totals['skipped']} skipped")
    return True


def push_rows(rows: list[dict], batch_size: int = 100, quiet: bool = False, send_options: bool = False) -> bool:
    """Upsert listing rows (as saved in listings.json) into the database.
    Returns True on success, False (after printing a warning) on any failure.
    With only the anon key this goes through submit_listings; send_options=True also
    submits looked-up options (used right after a lookup, not for bulk re-syncs)."""
    if not rows:
        return True
    base, key = read_env("SUPABASE_URL"), read_env("SUPABASE_SERVICE_KEY")
    if base and not key and read_env("SUPABASE_ANON_KEY"):
        return push_rows_as_contributor(rows, send_options=send_options, quiet=quiet)
    if not base or not key:
        print("  database not configured (SUPABASE_URL plus SUPABASE_SERVICE_KEY or SUPABASE_ANON_KEY): skipped")
        return False
    endpoint = base.rstrip("/") + "/rest/v1/listings?on_conflict=listing_key"

    payloads = [to_payload(to_params(r)) for r in rows]
    # PostgREST needs every row in one request to have the same columns, so rows
    # are grouped by which columns they carry.
    by_columns: dict[frozenset, list] = {}
    for p in payloads:
        by_columns.setdefault(frozenset(p), []).append(p)
    groups = list(by_columns.values())
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

"""
Help fill in factory options for the shared database.

Asks the database for VINs that still need a lookup (claiming them so nobody
else gets the same ones), looks each one up on bimmer.work in your own Chrome,
and sends the result back. Your own network gets its own bimmer.work limit, so
more helpers means more VINs done.

    python lookup_shared.py --dry-run               # how much work is left
    python lookup_shared.py                         # work until nothing is left or bimmer.work stops
    python lookup_shared.py --limit 10              # at most 10 lookups this run
    python lookup_shared.py --cooldown 15 --max-cooldowns 10

Needs SUPABASE_URL and SUPABASE_ANON_KEY (the public "anon" key, never the
secret one) in the environment or a .env file next to this script.

Results are sent as soon as each lookup finishes. If sending fails, the result is
kept in unsent_options.jsonl and retried next time, so a lookup is never wasted.
"""
import argparse
import json
import os
import socket
import sys
import time

import requests

try:                                   # company networks that inspect HTTPS
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

from bmw_lookup import BmwSession, add_browser_args, apply_browser_args
from db_push import read_env

UNSENT_FILE = "unsent_options.jsonl"


class Api:
    """The four functions the shared database exposes (see schema_shared.sql)."""

    def __init__(self, base: str, key: str):
        self.base = base.rstrip("/")
        self.headers = {"apikey": key, "Content-Type": "application/json"}
        if key.startswith("eyJ"):
            self.headers["Authorization"] = f"Bearer {key}"

    def call(self, name: str, args: dict, retries: int = 2):
        last = None
        for attempt in range(retries + 1):
            try:
                resp = requests.post(f"{self.base}/rest/v1/rpc/{name}", json=args,
                                     headers=self.headers, timeout=60)
                if resp.status_code < 300:
                    return resp.json() if resp.text else None
                last = RuntimeError(f"{name} failed ({resp.status_code}): {resp.text[:300]}")
                if resp.status_code < 500 and resp.status_code != 429:
                    raise last                       # our mistake or a rejection: retrying won't help
            except requests.RequestException as exc:
                last = exc
            if attempt < retries:
                time.sleep(2.0 * (attempt + 1))
        raise last


def flush_unsent(api: Api, worker: str) -> None:
    """Send results that could not be sent last time."""
    if not os.path.exists(UNSENT_FILE):
        return
    with open(UNSENT_FILE, encoding="utf-8") as f:
        items = [json.loads(line) for line in f if line.strip()]
    remaining = []
    for item in items:
        try:
            api.call("submit_options", {"p_vin": item["vin"], "p_build_sheet": item["build_sheet"]})
        except Exception as exc:
            if "build_sheet must" in str(exc) or "no options" in str(exc):
                continue                             # will never be accepted; drop it
            remaining.append(item)
    if remaining:
        with open(UNSENT_FILE, "w", encoding="utf-8") as f:
            f.writelines(json.dumps(i) + "\n" for i in remaining)
        print(f"{len(remaining)} earlier result(s) still could not be sent; kept in {UNSENT_FILE}.")
    else:
        os.remove(UNSENT_FILE)
        print(f"Sent {len(items)} result(s) saved from an earlier run.")


def send_result(api: Api, vin: str, build_sheet: dict) -> str:
    try:
        stored = api.call("submit_options", {"p_vin": vin, "p_build_sheet": build_sheet})
    except Exception as exc:
        with open(UNSENT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({"vin": vin, "build_sheet": build_sheet}) + "\n")
        print(f"  could not send the result ({exc}); saved to {UNSENT_FILE} to retry later.")
        return "unsent"
    return "stored" if stored else "already_done"


def work_round(api: Api, args, remaining_budget: int | None) -> dict:
    """One Chrome session: keep claiming small batches and looking them up until
    the queue is empty, the budget is used, or bimmer.work stops answering."""
    stats = {"stored": 0, "handled": 0, "failed": 0, "stopped_early": False, "queue_empty": False}
    in_a_row = 0
    first = True
    failed_vins: list[str] = []
    with BmwSession() as session:
        while True:
            want = args.claim_size if remaining_budget is None else min(args.claim_size, remaining_budget - stats["handled"] - stats["failed"])
            if want <= 0:
                return stats
            batch = api.call("claim_pending_vins", {
                "p_worker": args.worker, "p_limit": want, "p_min_year": args.min_year,
                "p_trim": args.trim, "p_make": args.make})
            if not batch:
                stats["queue_empty"] = True
                return stats
            for i, item in enumerate(batch):
                vin = item["listing_vin"]
                if not first:
                    time.sleep(args.delay)
                first = False
                print(f"{vin}  {item.get('listing_year')} {item.get('listing_title', '')[:50]}")
                try:
                    sheet = session.lookup(vin)
                except Exception as exc:
                    stats["failed"] += 1
                    failed_vins.append(vin)
                    in_a_row += 1
                    print(f"  failed: {exc.__class__.__name__}: {exc}")
                    if in_a_row >= args.max_failures:
                        stats["stopped_early"] = True
                        print(f"Stopping after {in_a_row} failures in a row (rate-limited or blocked?).")
                        # give back everything we could not finish: the failures and the untried rest
                        left = failed_vins + [b["listing_vin"] for b in batch[i + 1:]]
                        try:
                            api.call("release_vins", {"p_worker": args.worker, "p_vins": left})
                        except Exception:
                            pass                     # claims expire on their own after 30 minutes
                        return stats
                    continue
                in_a_row = 0
                stats["handled"] += 1
                outcome = send_result(api, vin, sheet)
                if outcome == "already_done":
                    print("  someone else already stored this one")
                elif outcome == "stored":
                    stats["stored"] += 1
                    print(f"  stored {len(sheet.get('Options', {}))} options")


def main():
    parser = argparse.ArgumentParser(description="Look up factory options for the shared database.")
    parser.add_argument("--min-year", type=int, default=2022)
    parser.add_argument("--trim", default="M3 xDrive Competition")
    parser.add_argument("--make", default="BMW")
    parser.add_argument("--limit", type=int, default=None, help="Look up at most this many VINs in this run")
    parser.add_argument("--claim-size", type=int, default=5, help="VINs to claim at a time (1-25)")
    parser.add_argument("--delay", type=float, default=10.0, help="Seconds between lookups")
    parser.add_argument("--max-failures", type=int, default=3, help="Stop after this many failed lookups in a row")
    parser.add_argument("--cooldown", type=float, default=0.0, help="If bimmer.work stops, wait this many minutes and continue (0 = just stop)")
    parser.add_argument("--max-cooldowns", type=int, default=3)
    parser.add_argument("--worker", default=socket.gethostname(), help="Name shown in the database for your claims")
    parser.add_argument("--dry-run", action="store_true", help="Show how much work is left and exit")
    add_browser_args(parser, default=True)
    args = parser.parse_args()
    apply_browser_args(args)

    base = read_env("SUPABASE_URL")
    key = read_env("SUPABASE_ANON_KEY") or read_env("SUPABASE_SERVICE_KEY")
    if not base or not key:
        sys.exit("Set SUPABASE_URL and SUPABASE_ANON_KEY (environment variable or .env file). See the README.")
    api = Api(base, key)

    counts = api.call("count_pending", {"p_min_year": args.min_year, "p_trim": args.trim, "p_make": args.make})
    c = counts[0] if isinstance(counts, list) else counts
    print(f"{args.make} {args.trim} {args.min_year}+: {c['pending']} waiting, {c['claimed']} being worked on, {c['done']} done.")
    if args.dry_run or not c["pending"]:
        return

    flush_unsent(api, args.worker)
    total = stored_total = 0
    cooldowns = 0
    while True:
        budget = None if args.limit is None else args.limit - total
        if budget is not None and budget <= 0:
            break
        stats = work_round(api, args, budget)
        total += stats["handled"] + stats["failed"]
        stored_total += stats["stored"]
        if stats["queue_empty"] or not stats["stopped_early"]:
            break
        print(f"bimmer.work stopped responding after {stats['stored']} successful lookup(s) in this round.")
        if args.cooldown <= 0 or cooldowns >= args.max_cooldowns:
            break
        cooldowns += 1
        print(f"Waiting {args.cooldown:g} minutes, then continuing ({cooldowns}/{args.max_cooldowns}). Ctrl+C is safe.")
        time.sleep(args.cooldown * 60)

    flush_unsent(api, args.worker)
    print(f"\nFinished: {stored_total} looked up and stored this run.")


if __name__ == "__main__":
    main()

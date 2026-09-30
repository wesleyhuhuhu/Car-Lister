"""
Local companion for the listings page's "Fetch options" button.

Run this on your own computer and leave it open. When you click "Fetch options"
on the page, the page asks this app (at http://127.0.0.1:8765) to look the VIN
up on bimmer.work in YOUR Chrome, on YOUR network. The result is sent to the
shared database and shows up on the page.

    python companion.py --allow-origin https://your-listings-page.example

Needs SUPABASE_URL and SUPABASE_ANON_KEY in the environment or a .env file.

Safety:
  * It only listens on 127.0.0.1 (this computer), never on the network.
  * Only web pages whose address you list with --allow-origin (or the
    COMPANION_ORIGINS environment variable, comma separated) may talk to it; any
    other website's request is refused.
  * One lookup at a time, with a pause between them. After a few failures in a
    row (bimmer.work is probably rate limiting you) it stops trying for a while
    instead of hammering the site.

Page API (all JSON):
  GET  /status          -> {"app","version","worker","busy","queued","blocked_until_minutes"}
  POST /fetch {"vin"}   -> 202 {"state":"queued"}          started
                           409 {"state":"unavailable"}      already has options / being fetched by someone else
                           429 {"state":"blocked", ...}     bimmer.work not answering; try later
                           400 bad VIN, 403 not an allowed page
  GET  /job?vin=...     -> {"state":"queued|running|done|failed","message":"..."}
"""
import argparse
import json
import os
import queue
import re
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from bmw_lookup import BmwSession, add_browser_args, apply_browser_args
from db_push import read_env
from lookup_shared import Api, send_result

VERSION = 1
VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")

sleep = time.sleep          # patched in tests
now = time.monotonic


class Worker(threading.Thread):
    """Owns the one Chrome session (Playwright objects must stay on the thread
    that created them) and runs fetch jobs one at a time."""

    def __init__(self, api: Api, name: str, delay: float, max_failures: int, cooldown_minutes: float, idle_seconds: float):
        super().__init__(daemon=True)
        self.api, self.name_, self.delay = api, name, delay
        self.max_failures, self.cooldown, self.idle = max_failures, cooldown_minutes * 60, idle_seconds
        self.jobs: queue.Queue = queue.Queue()
        self.state: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.running: str | None = None
        self.blocked_until = 0.0
        self.failures = 0
        self.last_lookup = 0.0
        self.stop_flag = threading.Event()

    # ---- called from request threads
    def blocked_minutes(self) -> float:
        return max(0.0, (self.blocked_until - now()) / 60)

    def submit(self, vin: str) -> bool:
        """Queue a VIN. False if it is already queued or running (double click)."""
        with self.lock:
            if self.state.get(vin, {}).get("state") in ("queued", "running"):
                return False
            self.state[vin] = {"state": "queued", "message": "Waiting for your turn."}
        self.jobs.put(vin)
        return True

    def status(self) -> dict:
        return {"app": "car-lister-companion", "version": VERSION, "worker": self.name_,
                "busy": self.running is not None, "queued": self.jobs.qsize(),
                "blocked_until_minutes": round(self.blocked_minutes(), 1)}

    def job(self, vin: str) -> dict:
        with self.lock:
            return dict(self.state.get(vin, {"state": "unknown", "message": "No such job."}))

    # ---- worker thread
    def _set(self, vin, state, message="", **extra):
        with self.lock:
            self.state[vin] = {"state": state, "message": message, **extra}

    def _release(self, vin):
        try:
            self.api.call("release_vins", {"p_worker": self.name_, "p_vins": [vin]})
        except Exception:
            pass                                    # the claim expires by itself after 30 minutes

    def run(self):
        session_cm, session = None, None
        try:
            while not self.stop_flag.is_set():
                try:
                    vin = self.jobs.get(timeout=self.idle)
                except queue.Empty:
                    if session_cm is not None:        # idle: close Chrome
                        session_cm.__exit__(None, None, None)
                        session_cm = session = None
                    continue
                if self.blocked_minutes() > 0:
                    self._release(vin)
                    self._set(vin, "failed", f"bimmer.work isn't answering; try again in {self.blocked_minutes():.0f} minutes.")
                    continue
                self.running = vin
                self._set(vin, "running", "Looking up on bimmer.work...")
                try:
                    if session is None:
                        session_cm = BmwSession()
                        session = session_cm.__enter__()
                    wait = self.delay - (now() - self.last_lookup)
                    if self.last_lookup and wait > 0:
                        sleep(wait)
                    sheet = session.lookup(vin)
                    self.last_lookup = now()
                except Exception as exc:
                    self.last_lookup = now()
                    self.failures += 1
                    self._release(vin)
                    msg = f"{exc.__class__.__name__}: {exc}"
                    if self.failures >= self.max_failures:
                        self.blocked_until = now() + self.cooldown
                        self.failures = 0
                        msg += f" (stopping for {self.cooldown / 60:.0f} minutes to stay under bimmer.work's limit)"
                        if session_cm is not None:
                            try:
                                session_cm.__exit__(None, None, None)
                            except Exception:
                                pass
                            session_cm = session = None
                    self._set(vin, "failed", msg)
                    self.running = None
                    continue
                self.failures = 0
                outcome = send_result(self.api, vin, sheet)
                if outcome == "stored":
                    self._set(vin, "done", "Options saved.", options=len(sheet.get("Options", {})))
                elif outcome == "already_done":
                    self._set(vin, "done", "Someone else saved these first.")
                else:
                    self._set(vin, "failed", "Looked up, but could not save; it will be sent next time lookup_shared.py runs.")
                self.running = None
        finally:
            if session_cm is not None:
                try:
                    session_cm.__exit__(None, None, None)
                except Exception:
                    pass


def make_handler(worker: Worker, api: Api, allowed_origins: set[str], port: int):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        server_version = "CarListerCompanion"

        def log_message(self, *a):
            pass

        # -- helpers
        def _origin_ok(self) -> bool:
            origin = self.headers.get("Origin")
            return origin is None or origin in allowed_origins

        def _send(self, code: int, body: dict | None = None):
            data = json.dumps(body if body is not None else {}).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            origin = self.headers.get("Origin")
            if origin and origin in allowed_origins:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
            self.end_headers()
            self.wfile.write(data)

        def _guard(self) -> bool:
            if self.headers.get("Host") not in allowed_hosts:      # blocks DNS-rebinding tricks
                self._send(403, {"error": "bad host"})
                return False
            if not self._origin_ok():
                self._send(403, {"error": "this page is not allowed to use the companion app"})
                return False
            return True

        # -- routes
        def do_OPTIONS(self):
            if not self._guard():
                return
            self.send_response(204)
            origin = self.headers.get("Origin")
            if origin:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Access-Control-Max-Age", "600")
            self.end_headers()

        def do_GET(self):
            if not self._guard():
                return
            url = urlparse(self.path)
            if url.path == "/status":
                return self._send(200, worker.status())
            if url.path == "/job":
                vin = (parse_qs(url.query).get("vin") or [""])[0].strip().upper()
                return self._send(200, worker.job(vin))
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._guard():
                return
            if urlparse(self.path).path != "/fetch":
                return self._send(404, {"error": "not found"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
                vin = str(json.loads(self.rfile.read(length) or b"{}").get("vin", "")).strip().upper()
            except (ValueError, AttributeError):
                return self._send(400, {"error": "send JSON like {\"vin\": \"...\"}"})
            if not VIN_RE.match(vin):
                return self._send(400, {"error": "that is not a valid 17-character VIN"})
            if worker.blocked_minutes() > 0:
                return self._send(429, {"state": "blocked", "retry_after_minutes": round(worker.blocked_minutes(), 1),
                                        "message": "bimmer.work isn't answering; the companion is pausing to stay under its limit."})
            if worker.job(vin)["state"] in ("queued", "running"):
                return self._send(202, {"state": worker.job(vin)["state"]})
            try:
                claimed = api.call("claim_vin", {"p_worker": worker.name_, "p_vin": vin})
            except Exception as exc:
                return self._send(502, {"error": f"could not reach the database: {exc}"})
            if not claimed:
                return self._send(409, {"state": "unavailable",
                                        "message": "This VIN already has options, is being fetched by someone else, or isn't a BMW in the database."})
            worker.submit(vin)
            self._send(202, {"state": "queued"})

    return Handler


def main():
    parser = argparse.ArgumentParser(description="Local companion for the listings page's Fetch options button.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--allow-origin", action="append", default=[],
                        help="Address of the listings page allowed to use this app, e.g. https://you.github.io (repeatable)")
    parser.add_argument("--worker", default=socket.gethostname(), help="Name shown in the database for your claims")
    parser.add_argument("--delay", type=float, default=10.0, help="Minimum seconds between lookups")
    parser.add_argument("--max-failures", type=int, default=3, help="Failures in a row before pausing")
    parser.add_argument("--cooldown", type=float, default=15.0, help="Minutes to pause after that")
    parser.add_argument("--idle-minutes", type=float, default=5.0, help="Close Chrome after this long with nothing to do")
    add_browser_args(parser, default=True)
    args = parser.parse_args()
    apply_browser_args(args)

    base = read_env("SUPABASE_URL")
    key = read_env("SUPABASE_ANON_KEY") or read_env("SUPABASE_SERVICE_KEY")
    if not base or not key:
        sys.exit("Set SUPABASE_URL and SUPABASE_ANON_KEY (environment variable or .env file). See the README.")
    origins = set(args.allow_origin) | {o.strip().rstrip("/") for o in (read_env("COMPANION_ORIGINS") or "").split(",") if o.strip()}
    origins = {o.rstrip("/") for o in origins}
    if not origins:
        print("Warning: no --allow-origin given, so no web page will be allowed to use this app yet.")

    api = Api(base, key)
    worker = Worker(api, args.worker, args.delay, args.max_failures, args.cooldown, args.idle_minutes * 60)
    worker.start()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(worker, api, origins, args.port))
    print(f"Companion running at http://127.0.0.1:{args.port} for: {', '.join(sorted(origins)) or 'nobody yet'}")
    print("Leave this window open. Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        worker.stop_flag.set()
        server.server_close()


if __name__ == "__main__":
    main()

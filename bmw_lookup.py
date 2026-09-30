from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright
from scrape_bmw_data import scrape_bmw_build_sheet
import oem_lookup

def find_chrome() -> Path | None:
    """Locate a Chromium-based browser on this machine. Set the CHROME_PATH
    environment variable (or .env-style shell setting) to force a specific one.
    Prefers Google Chrome, then falls back to Microsoft Edge / Chromium."""
    override = os.environ.get("CHROME_PATH")
    if override:
        return Path(override)

    candidates: list[Path] = []
    if sys.platform.startswith("win"):
        for var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(var)
            if base:
                candidates.append(Path(base) / "Google/Chrome/Application/chrome.exe")
        for var in ("PROGRAMFILES(X86)", "PROGRAMFILES"):
            base = os.environ.get(var)
            if base:
                candidates.append(Path(base) / "Microsoft/Edge/Application/msedge.exe")
    elif sys.platform == "darwin":
        candidates += [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
            Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
        ]
    for path in candidates:
        if path.exists():
            return path

    for name in ("google-chrome", "google-chrome-stable", "chrome",
                 "chromium", "chromium-browser", "microsoft-edge", "msedge"):
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def default_profile_dir() -> Path:
    """Where the automation browser keeps its own profile (cookies, cleared
    captchas). Chrome only allows remote debugging on a non-default profile, so
    this is a separate folder, created automatically for whoever runs the tool.
    Override with the CHROME_PROFILE environment variable."""
    override = os.environ.get("CHROME_PROFILE")
    if override:
        return Path(override)
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:                                   # Windows
        legacy = Path(local_app_data) / "ChromeAutomation"
        if legacy.exists():                              # keep an earlier setup working
            return legacy
        return Path(local_app_data) / "CarLister" / "ChromeProfile"
    return Path.home() / ".car-lister" / "chrome-profile"


CHROME_PATH = find_chrome()
CHROME_PROFILE = default_profile_dir()

BMW_URL = os.environ.get("BMW_URL", "https://bimmer.work/")
# Set BMW_HEADLESS=1 to run Chrome without a visible window. Some sites treat
# headless browsers differently and a human check can't be clicked in one, so
# the default is a normal window.
HEADLESS_DEFAULT = os.environ.get("BMW_HEADLESS", "").strip().lower() in ("1", "true", "yes")
CHROME_EXTRA_ARGS = os.environ.get("CHROME_EXTRA_ARGS", "").split()   # e.g. --no-sandbox on Linux

# Fallback site used when bimmer.work blocks us (see oem_lookup.py). OEM_FALLBACK=0 turns it off.
FALLBACK_DEFAULT = os.environ.get("OEM_FALLBACK", "1").strip().lower() not in ("0", "false", "no")
# After bimmer.work blocks us, go straight to the fallback for this many minutes.
BMW_RETRY_MINUTES = float(os.environ.get("BMW_RETRY_MINUTES", "10"))

_headless_override: bool | None = None
_fallback_override: bool | None = None


class SiteBlocked(RuntimeError):
    """bimmer.work would not answer (HTTP error, or no VIN box / Submit button). The
    only failure that triggers the fallback site; a VIN it doesn't know does not."""


def add_browser_args(parser, default: bool | None = None) -> None:
    """Add --headless / --show-browser and --no-fallback to a script. `default` is what
    the script does when neither window flag is given (None = follow BMW_HEADLESS,
    else a visible window)."""
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--headless", dest="headless", action="store_true", default=default,
                       help="Run Chrome without a visible window"
                       + (" (default)" if default else ""))
    group.add_argument("--show-browser", dest="headless", action="store_false",
                       help="Show the Chrome window while looking up"
                       + (" (default)" if default is False else ""))
    parser.add_argument("--no-fallback", dest="fallback", action="store_false", default=True,
                        help="Don't use oemnavigations.com when bimmer.work blocks the lookup")


def apply_browser_args(args) -> None:
    """Make every BmwSession() created afterwards follow the chosen options."""
    global _headless_override, _fallback_override
    _headless_override = getattr(args, "headless", None)
    _fallback_override = False if getattr(args, "fallback", True) is False else None


# earlier names
add_headless_args, apply_headless_args = add_browser_args, apply_browser_args

CDP_HOST = "127.0.0.1"
CDP_PORT = int(os.environ.get("CDP_PORT", "9222"))
CDP_URL = f"http://{CDP_HOST}:{CDP_PORT}"


def start_chrome(headless: bool = False) -> subprocess.Popen:
    """
    Start Chrome (or Edge) with a separate profile and remote debugging.
    """

    if CHROME_PATH is None or not CHROME_PATH.exists():
        raise FileNotFoundError(
            "Chrome was not found. Install Google Chrome, or set the "
            "CHROME_PATH environment variable to your chrome/msedge executable."
        )

    CHROME_PROFILE.mkdir(
        parents=True,
        exist_ok=True
    )

    command = [
        str(CHROME_PATH),
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={CHROME_PROFILE}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if headless:
        command += ["--headless=new", "--window-size=1280,900"]
    command += CHROME_EXTRA_ARGS

    print("Starting Google Chrome" + (" (headless)..." if headless else "..."))

    process = subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    return process


def wait_for_chrome(timeout: int = 15) -> None:
    """
    Wait until Chrome's remote-debugging endpoint becomes available.
    """

    print("Waiting for Chrome remote debugging...")

    start_time = time.time()

    while time.time() - start_time < timeout:
        try:
            with urllib.request.urlopen(
                f"{CDP_URL}/json/version",
                timeout=1,
            ) as response:

                if response.status == 200:
                    print("Chrome is ready.")
                    return

        except Exception:
            pass

        time.sleep(0.25)

    raise TimeoutError(
        "Chrome started, but remote debugging did not become "
        f"available at {CDP_URL}"
    )


class BmwSession:
    """
    One Chrome window reused for any number of VIN lookups.

        with BmwSession() as session:
            build_sheet = session.lookup("WBS...")

    Starting Chrome for every VIN is slow and can collide with the previous
    Chrome that is still shutting down, so batches share a single session.
    """

    def __init__(self, headless: bool | None = None, fallback: bool | None = None) -> None:
        if headless is None:
            headless = _headless_override if _headless_override is not None else HEADLESS_DEFAULT
        self.headless = headless
        if fallback is None:
            fallback = _fallback_override if _fallback_override is not None else FALLBACK_DEFAULT
        self.fallback = fallback
        self._bimmer_skip_until = 0.0
        self.last_status: int | None = None
        self._chrome: subprocess.Popen | None = None
        self._playwright = None
        self._browser = None
        self.page = None

    def _launch(self) -> None:
        try:
            self._chrome = start_chrome(self.headless)
            wait_for_chrome()

            print("Connecting Playwright to Chrome...")
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.connect_over_cdp(CDP_URL)

            context = self._browser.contexts[0]
            self.page = context.pages[0] if context.pages else context.new_page()
            print("Connected!")
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> "BmwSession":
        self._launch()
        return self

    def reopen(self, headless: bool) -> None:
        """Close Chrome and start it again with or without a visible window. The
        profile (cookies, visitor id) is the same, so sites see the same browser."""
        self.close()
        time.sleep(1)                      # let the old Chrome release the profile
        self.headless = headless
        self._launch()

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                pass
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
        if self._chrome is not None:
            # Shut down the Chrome process that Python started.
            self._chrome.terminate()
            try:
                self._chrome.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._chrome.kill()
        self._chrome = self._playwright = self._browser = self.page = None

    def snapshot(self, tag: str) -> dict:
        """Describe the page as it is right now and save a screenshot + HTML to
        bmw_debug/. Never raises; anything it can't read is left out."""
        page = self.page
        info: dict = {"status": self.last_status, "url": None, "title": None,
                      "buttons": None, "text": None, "png": None, "html": None, "error": None}
        try:
            debug_dir = Path("bmw_debug")
            debug_dir.mkdir(exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            png, html = debug_dir / f"{tag}-{stamp}.png", debug_dir / f"{tag}-{stamp}.html"
            page.screenshot(path=str(png), full_page=True)
            html.write_text(page.content(), encoding="utf-8")
            info["png"], info["html"] = str(png), str(html)
        except Exception as exc:
            info["error"] = f"could not save debug files: {exc}"
        try:
            info["url"], info["title"] = page.url, page.title()
            info["buttons"] = [t.strip() for t in page.get_by_role("button").all_inner_texts() if t.strip()]
        except Exception:
            pass
        try:
            info["text"] = " ".join(page.inner_text("body").split())[:400]
        except Exception:
            pass
        return info

    def _describe_missing_form(self, vin: str, headline: str | None = None) -> str:
        """The page did not show the VIN box / Submit button (or answered with an
        error code): describe it and point at the saved screenshot."""
        i = self.snapshot(vin)
        parts = [headline or "The VIN box or Submit button did not appear on bimmer.work (possibly a usage limit)."]
        if i["status"] is not None:
            parts.append(f"Page load: HTTP {i['status']}.")
        if i["png"]:
            parts.append(f"Saved {i['png']} and {i['html']}.")
        if i["error"]:
            parts.append(f"({i['error']})")
        if i["buttons"] is not None:
            parts.append(f"Buttons on the page: {i['buttons'] or 'none'}. URL: {i['url']}. Title: {i['title']!r}.")
        if i["text"]:
            parts.append(f"Page text: {i['text']!r}")
        return " ".join(parts)

    def lookup(self, vin: str):
        """Look a VIN up on bimmer.work; if that site blocks us, try oemnavigations.com
        (unless disabled). The returned build sheet says which site answered ("Source")."""
        blocked: SiteBlocked | None = None
        if not self.fallback or time.time() >= self._bimmer_skip_until:
            try:
                sheet = self._lookup_bimmer(vin)
                sheet.setdefault("Source", "bimmer.work")
                return sheet
            except SiteBlocked as exc:
                if not self.fallback:
                    raise
                blocked = exc
                self._bimmer_skip_until = time.time() + BMW_RETRY_MINUTES * 60
                print("bimmer.work is blocking lookups; switching to the fallback site "
                      f"(bimmer.work is skipped for {BMW_RETRY_MINUTES:g} minutes).")
        try:
            return oem_lookup.lookup(self, vin)
        except oem_lookup.OemLimitReached as exc:
            detail = f" First problem: {blocked}" if blocked else ""
            raise SiteBlocked(f"bimmer.work is blocking lookups and the fallback is used up. {exc}.{detail}")

    def _lookup_bimmer(self, vin: str):
        page = self.page

        response = page.goto(
            BMW_URL,
            wait_until="domcontentloaded",
        )
        self.last_status = response.status if response is not None else None
        if self.last_status is not None and self.last_status >= 400:
            meaning = " (Too Many Requests: you are being rate limited)" if self.last_status == 429 else ""
            raise SiteBlocked(self._describe_missing_form(
                vin, f"bimmer.work answered HTTP {self.last_status}{meaning}."))

        print(f"Entering VIN: {vin}")

        submit = page.get_by_role("button", name="Submit", exact=True)
        try:
            page.get_by_role("textbox").first.wait_for(state="visible", timeout=20_000)
            page.get_by_role("textbox").first.fill(vin)
            submit.wait_for(state="visible", timeout=20_000)
        except PlaywrightTimeoutError:
            raise SiteBlocked(self._describe_missing_form(vin))

        submit.click()

        page.wait_for_url(
            "**/vin/**",
            wait_until="domcontentloaded",
            timeout=30_000,
        )
        print("Vehicle page loaded.")
        build_sheet = scrape_bmw_build_sheet(page)

        print("Lookup successful!")
        return build_sheet


def lookup_bmw_vin(vin: str):
    """Look up a single VIN (starts and stops its own Chrome)."""
    with BmwSession() as session:
        return session.lookup(vin)

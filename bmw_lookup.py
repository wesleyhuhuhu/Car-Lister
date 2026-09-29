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

CDP_HOST = "127.0.0.1"
CDP_PORT = int(os.environ.get("CDP_PORT", "9222"))
CDP_URL = f"http://{CDP_HOST}:{CDP_PORT}"


def start_chrome() -> subprocess.Popen:
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

    print("Starting Google Chrome...")

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

    def __init__(self) -> None:
        self._chrome: subprocess.Popen | None = None
        self._playwright = None
        self._browser = None
        self.page = None

    def __enter__(self) -> "BmwSession":
        try:
            self._chrome = start_chrome()
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
        return self

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

    def _describe_missing_form(self, vin: str) -> str:
        """The page did not show the VIN box / Submit button. Save a screenshot
        and the HTML for inspection and describe what the page does contain."""
        page = self.page
        debug_dir = Path("bmw_debug")
        details = []
        try:
            debug_dir.mkdir(exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            png, html = debug_dir / f"{vin}-{stamp}.png", debug_dir / f"{vin}-{stamp}.html"
            page.screenshot(path=str(png), full_page=True)
            html.write_text(page.content(), encoding="utf-8")
            details.append(f"Saved {png} and {html}.")
        except Exception as exc:
            details.append(f"(could not save debug files: {exc})")
        try:
            buttons = [t.strip() for t in page.get_by_role("button").all_inner_texts() if t.strip()]
            details.append(f"Buttons on the page: {buttons or 'none'}. URL: {page.url}. Title: {page.title()!r}.")
        except Exception:
            pass
        try:
            text = " ".join(page.inner_text("body").split())
            details.append(f"Page text: {text[:400]!r}")
        except Exception:
            pass
        return ("The VIN box or Submit button did not appear on bimmer.work "
                "(possibly a usage limit). " + " ".join(details))

    def lookup(self, vin: str):
        page = self.page

        page.goto(
            "https://bimmer.work/",
            wait_until="domcontentloaded",
        )

        print(f"Entering VIN: {vin}")

        submit = page.get_by_role("button", name="Submit", exact=True)
        try:
            page.get_by_role("textbox").first.wait_for(state="visible", timeout=20_000)
            page.get_by_role("textbox").first.fill(vin)
            submit.wait_for(state="visible", timeout=20_000)
        except PlaywrightTimeoutError:
            raise RuntimeError(self._describe_missing_form(vin))

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

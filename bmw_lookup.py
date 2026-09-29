from __future__ import annotations

import subprocess
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright
from scrape_bmw_data import scrape_bmw_build_sheet

CHROME_PATH = Path(
    r"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"
)

CHROME_PROFILE = Path(
    r"C:\\Users\\wesle\\AppData\\Local\\ChromeAutomation"
)

CDP_HOST = "127.0.0.1"
CDP_PORT = 9222
CDP_URL = f"http://{CDP_HOST}:{CDP_PORT}"


def start_chrome() -> subprocess.Popen:
    """
    Start Google Chrome with a separate profile and remote debugging.
    """

    if not CHROME_PATH.exists():
        raise FileNotFoundError(
            f"Chrome was not found at:\n{CHROME_PATH}"
        )

    CHROME_PROFILE.mkdir(
        parents=True,
        exist_ok=True
    )

    command = [
        str(CHROME_PATH),
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={CHROME_PROFILE}",
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

    def lookup(self, vin: str):
        page = self.page

        page.goto(
            "https://bimmer.work/",
            wait_until="domcontentloaded",
        )

        print(f"Entering VIN: {vin}")

        page.get_by_role("textbox").first.fill(vin)

        page.get_by_role(
            "button",
            name="Submit",
            exact=True,
        ).click()

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

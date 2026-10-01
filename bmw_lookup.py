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
    this is a separate folder, created automatically for whoever runs the tool:
    %LOCALAPPDATA%/ChromeAutomation on Windows. Override with the CHROME_PROFILE
    environment variable."""
    override = os.environ.get("CHROME_PROFILE")
    if override:
        return Path(override)
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:                                   # Windows
        return Path(local_app_data) / "ChromeAutomation"
    return Path.home() / ".car-lister" / "chrome-profile"


CHROME_PATH = find_chrome()
CHROME_PROFILE = default_profile_dir()


def default_template_dir() -> Path | None:
    """A prepared Chrome profile whose contents are copied into the automation profile each time it
    is reset: %LOCALAPPDATA%/Chrome Temp when that folder exists. Set CHROME_PROFILE_TEMPLATE to use
    another folder, or to "none" to start from an empty profile instead."""
    override = os.environ.get("CHROME_PROFILE_TEMPLATE")
    if override is not None:
        override = override.strip()
        return None if override.lower() in ("", "0", "none", "off") else Path(override)
    local_app_data = os.environ.get("LOCALAPPDATA")
    candidate = Path(local_app_data) / "Chrome Temp" if local_app_data else Path.home() / ".car-lister" / "chrome-template"
    return candidate if candidate.is_dir() else None


PROFILE_TEMPLATE = default_template_dir()
# Chrome's "this profile is open" markers: copying them would make the new Chrome think it is already running.
TEMPLATE_SKIP = {"lockfile", "SingletonLock", "SingletonCookie", "SingletonSocket", "RunningChromeVersion"}

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

# Start every VIN with an empty automation profile (no cookies or cache left over from the previous VIN),
# so each lookup runs the same way. --keep-profile or CHROME_FRESH_PROFILE=0 turns it off.
FRESH_PROFILE_DEFAULT = os.environ.get("CHROME_FRESH_PROFILE", "1").strip().lower() not in ("0", "false", "no")
# Only folders with these names are ever emptied, so a mistaken CHROME_PROFILE can't wipe anything else.
WIPEABLE_PROFILE_NAMES = {"ChromeAutomation", "ChromeProfile", "chrome-profile"}

_headless_override: bool | None = None
_fallback_override: bool | None = None
_fresh_override: bool | None = None


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
    parser.add_argument("--keep-profile", dest="fresh_profile", action="store_false", default=None,
                        help="Keep the Chrome automation profile between VINs (default: empty it before every VIN)")


def apply_browser_args(args) -> None:
    """Make every BmwSession() created afterwards follow the chosen options."""
    global _headless_override, _fallback_override, _fresh_override
    _headless_override = getattr(args, "headless", None)
    _fallback_override = False if getattr(args, "fallback", True) is False else None
    _fresh_override = getattr(args, "fresh_profile", None)


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


def _stop_chrome(process: subprocess.Popen) -> None:
    """Stop the Chrome that Python started, including its helper processes. If any
    helper survives it keeps the profile locked, and the next Chrome started with
    the same profile would hand over to it and ignore its flags (e.g. no window)."""
    if process.poll() is None:
        if sys.platform.startswith("win"):
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


def wait_for_chrome_gone(timeout: int = 15) -> bool:
    """Wait until nothing answers on the remote-debugging port any more."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"{CDP_URL}/json/version", timeout=1).close()
        except Exception:
            time.sleep(0.5)          # let the profile lock be released too
            return True
        time.sleep(0.25)
    return False


def _chrome_answering() -> bool:
    try:
        urllib.request.urlopen(f"{CDP_URL}/json/version", timeout=1).close()
        return True
    except Exception:
        return False


def stop_profile_chrome() -> None:
    """Stop any Chrome/Edge that is using the automation profile (a leftover from an
    earlier run). Only processes started with this tool's own --user-data-dir are
    touched; your normal Chrome is never affected."""
    profile = str(CHROME_PROFILE)
    print(f"Stopping a leftover automation Chrome that is still using {profile} ...")
    try:
        if sys.platform.startswith("win"):
            script = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and "
                      "$_.CommandLine.Contains($env:CL_PROFILE) -and $_.Name -match 'chrome|msedge' } | "
                      "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
            subprocess.run(["powershell", "-NoProfile", "-Command", script], env={**os.environ, "CL_PROFILE": profile},
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        else:
            subprocess.run(["pkill", "-f", "--", f"--user-data-dir={profile}"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    except Exception as exc:
        print(f"(could not stop it automatically: {exc})")
    wait_for_chrome_gone(10)


def wipe_profile() -> None:
    """Empty the automation profile folder (the folder itself stays). Refuses any folder that isn't
    one of this tool's own profiles. Chrome must be closed first; files Windows still has locked are
    retried for a few seconds."""
    profile = CHROME_PROFILE.resolve()
    if profile.name not in WIPEABLE_PROFILE_NAMES or len(profile.parts) < 3:
        raise RuntimeError(f"Refusing to empty {profile}: it is not a Car Lister automation profile "
                           f"(expected a folder named one of {sorted(WIPEABLE_PROFILE_NAMES)}).")
    if not profile.exists():
        profile.mkdir(parents=True, exist_ok=True)
        return
    if _chrome_answering():
        stop_profile_chrome()
    leftover: list[Path] = []
    for attempt in range(6):
        leftover = []
        for child in profile.iterdir():
            try:
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink()
            except OSError:
                leftover.append(child)
        if not leftover:
            return
        time.sleep(1)
    raise RuntimeError(f"Could not empty the Chrome profile {profile}; still in use: "
                       f"{', '.join(p.name for p in leftover[:5])}. Close any Chrome using it and try again.")


def seed_profile() -> None:
    """Copy the template profile's contents (see PROFILE_TEMPLATE) into the automation profile.
    The template is only read, never changed."""
    if PROFILE_TEMPLATE is None:
        return
    src, dst = PROFILE_TEMPLATE.resolve(), CHROME_PROFILE.resolve()
    if not src.is_dir():
        raise RuntimeError(f"The Chrome profile template {src} doesn't exist. Fix CHROME_PROFILE_TEMPLATE, "
                           f"or set it to none to start from an empty profile.")
    if src == dst or src in dst.parents or dst in src.parents:
        raise RuntimeError(f"The profile template {src} and the automation profile {dst} must be separate folders.")
    print(f"Copying the Chrome profile template {src} into {dst} ...")
    try:
        shutil.copytree(src, dst, dirs_exist_ok=True,
                        ignore=lambda _dir, names: [n for n in names if n in TEMPLATE_SKIP])
    except shutil.Error as exc:
        failed = [str(item[0]) for item in (exc.args[0] if exc.args and isinstance(exc.args[0], list) else [])][:3]
        raise RuntimeError(f"Could not copy the profile template {src}: {failed or exc}. "
                           f"If a Chrome window is using {src.name}, close it and try again.")


def reset_profile() -> None:
    """Empty the automation profile, then fill it from the template (if there is one)."""
    wipe_profile()
    seed_profile()


def _profile_is_empty() -> bool:
    return not CHROME_PROFILE.exists() or not any(CHROME_PROFILE.iterdir())


class BmwSession:
    """
    One Chrome window reused for any number of VIN lookups.

        with BmwSession() as session:
            build_sheet = session.lookup("WBS...")

    Starting Chrome for every VIN is slow and can collide with the previous
    Chrome that is still shutting down, so batches share a single session.
    """

    def __init__(self, headless: bool | None = None, fallback: bool | None = None,
                 fresh_profile: bool | None = None) -> None:
        if headless is None:
            headless = _headless_override if _headless_override is not None else HEADLESS_DEFAULT
        self.headless = headless
        if fallback is None:
            fallback = _fallback_override if _fallback_override is not None else FALLBACK_DEFAULT
        self.fallback = fallback
        if fresh_profile is None:
            fresh_profile = _fresh_override if _fresh_override is not None else FRESH_PROFILE_DEFAULT
        self.fresh_profile = fresh_profile
        self._lookups_started = 0
        self._bimmer_skip_until = 0.0
        self.last_status: int | None = None
        self._chrome: subprocess.Popen | None = None
        self._playwright = None
        self._browser = None
        self.page = None

    def _launch(self) -> None:
        try:
            if _chrome_answering():
                # Something already owns the debugging port: a Chrome left over from an
                # earlier run. A new Chrome would just hand over to it (ignoring
                # --headless / window settings), so clear it out first.
                stop_profile_chrome()
                if _chrome_answering():
                    raise RuntimeError(
                        f"Another program is already using the debugging port {CDP_PORT}, and it is not "
                        f"a Chrome using {CHROME_PROFILE}. Close it or set CDP_PORT to another number.")
            self._chrome = start_chrome(self.headless)
            wait_for_chrome()
            time.sleep(1)
            if self._chrome.poll() is not None:
                raise RuntimeError(
                    "Chrome handed over to a Chrome that was already running with the same profile "
                    f"({CHROME_PROFILE}), so the requested window mode was ignored. Close every "
                    "Chrome window that uses that profile and try again.")

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
        if self.fresh_profile:
            reset_profile()
        elif PROFILE_TEMPLATE is not None and _profile_is_empty():
            seed_profile()                         # --keep-profile: fill it from the template once
        self._launch()
        return self

    def _restart_with_empty_profile(self) -> None:
        """Close Chrome, reset the profile (empty it, copy the template in) and start Chrome again."""
        what = f"from {PROFILE_TEMPLATE.name}" if PROFILE_TEMPLATE is not None else "to empty"
        print(f"Resetting the Chrome profile {CHROME_PROFILE} {what} for this VIN...")
        self.close()
        if not wait_for_chrome_gone(5):
            stop_profile_chrome()
        reset_profile()
        self._launch()

    def reopen(self, headless: bool) -> None:
        """Close Chrome and start it again with or without a visible window. The
        profile (cookies, visitor id) is the same, so sites see the same browser."""
        self.close()
        if not wait_for_chrome_gone(2):
            stop_profile_chrome()
        if _chrome_answering():
            raise RuntimeError("The previous Chrome is still running, so it can't be reopened "
                               f"{'hidden' if headless else 'with a window'}. Close all Chrome windows "
                               f"that use the profile {CHROME_PROFILE}.")
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
            _stop_chrome(self._chrome)
            wait_for_chrome_gone()
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
        if self.fresh_profile and self._lookups_started:     # the first VIN already got an empty profile at start
            self._restart_with_empty_profile()
        self._lookups_started += 1
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

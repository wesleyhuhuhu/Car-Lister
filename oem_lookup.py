"""
Fallback VIN check: https://oemnavigations.com/pages/vin-decoder-app

Used only when bimmer.work refuses to answer (rate limit / HTTP 429 / no VIN
box). The site allows about 2 free lookups per day per visitor, so:

  * a small local counter (~/.car-lister/oem_usage.json) records how many checks
    were tried per UTC day. It is information only and never blocks a lookup;
    the site itself enforces the limit and its message is reported as-is, and
  * if the site shows a human check (Cloudflare Turnstile) it is NOT bypassed:
    a hidden Chrome is closed and reopened with a visible window so the person
    can click it, then hidden again once the lookup is done.

The result is converted into the same "build sheet" shape bimmer.work produces
({"Options": {"248": "Steering wheel heater", ...}, "Color": ..., ...}) so the
rest of the pipeline (JSON/CSV, database, listings page) does not care which
site answered. Option codes are normalised to bimmer.work's style: this site
shows "0248" / "01CB", bimmer.work shows "248" / "1CB".
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from rebrowser_playwright.sync_api import TimeoutError as PlaywrightTimeoutError

OEM_URL = os.environ.get("OEM_URL", "https://oemnavigations.com/pages/vin-decoder-app")
OEM_DAILY_LIMIT = int(os.environ.get("OEM_DAILY_LIMIT", "2"))
USAGE_FILE = Path(os.environ.get("OEM_USAGE_FILE", str(Path.home() / ".car-lister" / "oem_usage.json")))
SOURCE = "oemnavigations.com"

RESULT_WAIT_SECONDS = 90          # the site answers via a websocket message; its own timeout is 60s
CAPTCHA_WAIT_SECONDS = 75         # how long a person gets to click the human check in a visible window


class OemLimitReached(RuntimeError):
    """oemnavigations.com has no free checks left (locally counted or told by the site)."""


# --------------------------------------------------------------------------- daily counter

def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def checks_used_today() -> int:
    try:
        data = json.loads(USAGE_FILE.read_text(encoding="utf-8"))
        return int(data["used"]) if data.get("date") == _today() else 0
    except Exception:
        return 0


def checks_left_today() -> int:
    return max(0, OEM_DAILY_LIMIT - checks_used_today())


def _write_used(used: int) -> None:
    try:
        USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
        USAGE_FILE.write_text(json.dumps({"date": _today(), "used": used}), encoding="utf-8")
    except Exception:
        pass  # the counter is a courtesy; the site enforces the real limit


def record_use() -> None:
    _write_used(checks_used_today() + 1)


def mark_exhausted() -> None:
    _write_used(max(checks_used_today(), OEM_DAILY_LIMIT))


# --------------------------------------------------------------------------- parsing

def normalize_code(code: str) -> str:
    """'01CB' -> '1CB', '0248' -> '248' (bimmer.work style). Other codes are kept."""
    code = (code or "").strip().upper()
    if len(code) == 4 and code.startswith("0"):
        return code[1:]
    return code


def parse_result(page) -> dict:
    """Read the result block of the page into plain Python:
    {"sections": {title: {label: value}}, "equipment": {title: [(code, text), ...]}}."""
    return page.evaluate(
        """() => {
            const root = document.querySelector('#vin-search-result');
            const out = {sections: {}, equipment: {}};
            if (!root) return out;
            for (const sec of root.querySelectorAll('.section')) {
                const title = (sec.querySelector('.section-title') || {}).textContent || '';
                const t = title.trim();
                const pairs = {};
                for (const p of sec.querySelectorAll('.data-pair')) {
                    const l = (p.querySelector('.data-label') || {}).textContent || '';
                    const v = (p.querySelector('.data-value') || {}).textContent || '';
                    if (l.trim()) pairs[l.trim().replace(/:$/, '')] = v.trim();
                }
                const items = [];
                for (const it of sec.querySelectorAll('.equipment-item')) {
                    const c = (it.querySelector('.equipment-code') || {}).textContent || '';
                    const text = it.textContent.trim();
                    items.push([c.trim(), text.startsWith(c.trim()) ? text.slice(c.trim().length).trim() : text]);
                }
                if (Object.keys(pairs).length) out.sections[t] = pairs;
                if (items.length) out.equipment[t] = items;
            }
            return out;
        }"""
    )


def to_build_sheet(parsed: dict) -> dict:
    """Convert parse_result() output into the shared build-sheet shape."""
    sections = parsed.get("sections") or {}
    equipment = parsed.get("equipment") or {}

    ex_works = next((v for k, v in equipment.items() if "ex works" in k.lower()), [])
    retrofitted = next((v for k, v in equipment.items() if "retrofit" in k.lower()), [])
    if not ex_works:
        raise RuntimeError("oemnavigations.com returned a result without an equipment list.")

    options = {normalize_code(code): text for code, text in ex_works if code}

    details: dict[str, str] = {}
    for title, pairs in sections.items():
        for label, value in pairs.items():
            if label not in ("VIN",) and value:
                details[label] = value

    # The equipment list also carries the paint and upholstery codes, e.g.
    # X3SW "Full leather Merino/black"; use that text for the upholstery.
    upholstery = details.get("Upholstery Code")
    upholstery_text = next((text for code, text in ex_works if code.upper() == (upholstery or "").upper()), None)

    sheet: dict = {}
    if details.get("Color"):
        sheet["Color"] = details["Color"]
    if upholstery:
        sheet["Upholstery"] = upholstery_text or upholstery
    if details.get("Production Date"):
        sheet["Start of Production"] = details["Production Date"]
    sheet["Options"] = options
    if retrofitted:
        sheet["Retrofitted"] = {normalize_code(c): t for c, t in retrofitted if c}
    sheet["Details"] = details
    sheet["Source"] = SOURCE
    return sheet


# --------------------------------------------------------------------------- driving the page

_LIMIT_WORDS = re.compile(r"limit|no (more )?(free )?(checks|lookups|searches)|remaining|too many|come back|tomorrow", re.I)


def _page_state(page) -> tuple[str, str]:
    """('result' | 'captcha' | 'error' | 'waiting', text)."""
    return page.evaluate(
        """() => {
            const res = document.querySelector('#vin-search-result');
            if (res && res.querySelector('.section-title')) return ['result', ''];
            const cap = document.querySelector('#captcha-container');
            if (cap && getComputedStyle(cap).display !== 'none' && cap.children.length) return ['captcha', ''];
            if (res && getComputedStyle(res).display !== 'none' && res.innerText.trim())
                return ['error', res.innerText.trim().slice(0, 300)];
            return ['waiting', ''];
        }"""
    )


class _NeedsVisibleWindow(Exception):
    pass


def lookup(session, vin: str) -> dict:
    """Run one lookup on oemnavigations.com using the session's browser page.
    If the site shows a human check while Chrome is hidden, Chrome is reopened with a
    visible window so the person can click it, then the lookup is repeated (the check
    appears before the VIN is accepted, so this doesn't cost an extra free check), and
    the window is hidden again afterwards.
    Returns a build sheet; raises OemLimitReached / RuntimeError."""
    was_headless = session.headless
    try:
        try:
            return _lookup(session, vin)
        except _NeedsVisibleWindow:
            print("oemnavigations.com wants a human check: reopening Chrome with a visible window. "
                  "Please tick the box in that window.")
            try:
                session.reopen(headless=False)
            except Exception as exc:
                raise RuntimeError(f"oemnavigations.com wants a human check and Chrome could not be opened "
                                   f"with a visible window: {exc}")
            return _lookup(session, vin)
    finally:
        if was_headless and not session.headless:
            try:
                session.reopen(headless=True)
            except Exception as exc:
                print(f"(could not hide Chrome again: {exc})")


def _lookup(session, vin: str) -> dict:
    page = session.page
    print(f"Trying fallback site {OEM_URL} ...")
    response = page.goto(OEM_URL, wait_until="domcontentloaded")
    status = response.status if response is not None else None
    if status is not None and status >= 400:
        raise RuntimeError(session._describe_missing_form(vin, f"oemnavigations.com answered HTTP {status}."))

    try:
        box = page.locator("#vinNumber")
        box.wait_for(state="visible", timeout=20_000)
        if page.locator("#maintenance-banner").is_visible():
            raise RuntimeError("oemnavigations.com says its VIN decoder is under maintenance.")
        box.fill(vin)
        page.locator("#submit-vin-search").click(timeout=10_000)
    except PlaywrightTimeoutError:
        raise RuntimeError(session._describe_missing_form(vin, "The oemnavigations.com VIN form did not appear."))

    deadline = time.time() + RESULT_WAIT_SECONDS
    captcha_deadline = None
    while time.time() < deadline:
        state, text = _page_state(page)
        if state == "result":
            break
        if state == "error":
            if _LIMIT_WORDS.search(text):
                mark_exhausted()
                raise OemLimitReached(f"oemnavigations.com: {text}")
            raise RuntimeError(f"oemnavigations.com: {text}")
        if state == "captcha":
            if session.headless:
                raise _NeedsVisibleWindow()
            if captcha_deadline is None:
                captcha_deadline = time.time() + CAPTCHA_WAIT_SECONDS
                deadline = max(deadline, captcha_deadline + 15)     # the check gets its full time
                print("oemnavigations.com is showing a human check. Please tick it in the browser window.")
            if time.time() > captcha_deadline:
                raise RuntimeError(session._describe_missing_form(
                    vin, "oemnavigations.com's human check did not complete. It often fails to load or verify in an "
                         "automated browser, and this VIN cannot be checked until it does. If opening the site by hand "
                         "says the 2 daily checks are used up, that limit is the real reason; try again after it resets."))
        page.wait_for_timeout(500)
    else:
        raise RuntimeError(session._describe_missing_form(vin, "oemnavigations.com gave no answer in time."))

    record_use()
    sheet = to_build_sheet(parse_result(page))
    print(f"Fallback lookup successful ({len(sheet['Options'])} options).")
    return sheet

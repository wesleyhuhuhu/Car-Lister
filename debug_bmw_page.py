"""
Debug helper: captures what bimmer.work actually renders for one VIN, so the
parsers in scrape_bmw_data.py can be checked against real page text/markup.

    python debug_bmw_page.py WBS...your17charVIN

Saves, for the vehicle page and the options page:
    bmw_vehicle.html / bmw_vehicle_text.txt / bmw_vehicle.png
    bmw_options.html / bmw_options_text.txt / bmw_options.png

If the site shows a verification step in the Chrome window, complete it
yourself there; this script just waits (up to 2 minutes) for the vehicle page.
"""
import sys

from rebrowser_playwright.sync_api import sync_playwright

from bmw_lookup import CDP_URL, start_chrome, wait_for_chrome
from scrape_bmw_data import options_url_for


def dump(page, stem: str) -> None:
    with open(f"{stem}.html", "w", encoding="utf-8") as f:
        f.write(page.content())
    text = page.locator("body").inner_text()
    with open(f"{stem}_text.txt", "w", encoding="utf-8") as f:
        f.write(text)
    page.screenshot(path=f"{stem}.png", full_page=True)
    print(f"Saved {stem}.html, {stem}_text.txt, {stem}.png ({len(text.splitlines())} text lines)")


def main() -> None:
    vin = sys.argv[1] if len(sys.argv) > 1 else input("BMW VIN: ").strip()
    proc = start_chrome()
    try:
        wait_for_chrome()
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(CDP_URL)
            context = browser.contexts[0]
            page = context.pages[0] if context.pages else context.new_page()

            page.goto("https://bimmer.work/", wait_until="domcontentloaded")
            page.get_by_role("textbox").first.fill(vin)
            page.get_by_role("button", name="Submit", exact=True).click()

            print("Waiting for the vehicle page...")
            page.wait_for_url("**/vin/**", wait_until="domcontentloaded", timeout=120_000)
            page.wait_for_timeout(25_000)  # the site takes roughly 15-18 s to process a VIN
            dump(page, "bmw_vehicle")

            page.goto(options_url_for(page.url), wait_until="domcontentloaded")
            page.wait_for_timeout(5_000)
            dump(page, "bmw_options")

            browser.close()
    finally:
        proc.terminate()


if __name__ == "__main__":
    main()

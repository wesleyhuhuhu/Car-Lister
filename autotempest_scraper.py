"""
Scrapes AutoTempest.com search results.

AutoTempest renders its results with client-side JS, so we use Playwright
rather than plain requests. Search params map directly to AutoTempest's
query string, e.g.:

    https://www.autotempest.com/results?make=toyota&model=camry&zip=90001&rad=50&minprice=10000&maxprice=25000

Common params: make, model, zip, rad (radius, miles), minprice, maxprice,
minyear, maxyear, minmiles, maxmiles.

The page loads each source (Carvana, Cars.com, eBay, CarSoup, Hemmings,
"Other" i.e. CarGurus/AutoByTel/etc.) via a separate async fetch after the
initial page load, so we wait for the first row to appear and then give the
page extra time (and optionally a few clicks) for the other sources to
finish loading before we scrape.

NOTE: AutoTempest's DOM structure/class names can change at any time.
If SELECTORS or their children come back empty, run debug_selectors.py to
dump the live rendered HTML and update the selectors below accordingly.
"""
import re
import urllib.parse
from typing import Optional

from playwright.sync_api import sync_playwright

from models import Listing

BASE_URL = "https://www.autotempest.com/results"

# Each result card is a <section class="search-result ..."> inside a
# <li class="result-list-item">. This markup is shared by every source's
# results section (#cs-results, #cv-results, #cm-results, #eb-results,
# #hem-results, #ot-results), so one selector covers all of them.
SELECTORS = {
    "row": "li.result-list-item section.search-result",
    "title_link": ".title-wrap.listing-title a.source-link",
    "price": ".badge__label.label--price",
    "mileage": ".info.mileageDate .mileage",
    "date": ".info.mileageDate .date",
    "city": ".info.location .city",
    "dealer": ".dealerName",
    "image": ".image",  # read the 'data-img' attribute
    "share_button": ".share-link button",  # onclick carries the VIN
}

# VIN is embedded in the Share button's onclick, e.g.:
#   window.AT.shareListing(`cs`, `4T1KZ1AK8LU045992`, `Toyota`, `camry`)
VIN_FROM_ONCLICK_RE = re.compile(r"shareListing\(`[^`]*`,\s*`([A-Z0-9]{11,17})`")


def build_search_url(**params) -> str:
    """Build an AutoTempest search URL from keyword params.

    AutoTempest's own links sort query params alphabetically by key (e.g.
    make, maxyear, minyear, model, radius, zip), so we do the same rather
    than hardcoding one order.

    Example:
        build_search_url(make="toyota", model="camry", zip="90001",
                          radius=50, minyear=2018, maxyear=2023)
    """
    query = {k: v for k, v in params.items() if v is not None}
    sorted_query = {k: query[k] for k in sorted(query)}
    return f"{BASE_URL}?{urllib.parse.urlencode(sorted_query)}"


def _extract_row(page, section) -> Optional[Listing]:
    title_a = section.query_selector(SELECTORS["title_link"])
    if not title_a:
        return None
    url = title_a.get_attribute("href")
    if not url:
        return None
    if url.startswith("/"):
        url = "https://www.autotempest.com" + url
    title = (title_a.inner_text() or "").strip()

    price_el = section.query_selector(SELECTORS["price"])
    price = None
    if price_el:
        # The price element can contain a nested tooltip with price-history
        # rows; grab only the element's own first text node.
        price = page.evaluate(
            "(el) => (el.childNodes[0] && el.childNodes[0].textContent || el.textContent).trim()",
            price_el,
        )

    mileage_el = section.query_selector(SELECTORS["mileage"])
    mileage = mileage_el.inner_text().strip() if mileage_el else None

    city_el = section.query_selector(SELECTORS["city"])
    location = city_el.inner_text().strip() if city_el else None
    dealer_el = section.query_selector(SELECTORS["dealer"])
    if dealer_el:
        dealer = dealer_el.inner_text().strip()
        location = f"{location} — {dealer}" if location else dealer

    img_el = section.query_selector(SELECTORS["image"])
    image_url = img_el.get_attribute("data-img") if img_el else None

    sitecode = section.get_attribute("data-backend-sitecode")

    vin = None
    share_btn = section.query_selector(SELECTORS["share_button"])
    if share_btn:
        onclick = share_btn.get_attribute("onclick") or ""
        m = VIN_FROM_ONCLICK_RE.search(onclick)
        if m:
            vin = m.group(1)

    return Listing(
        title=title,
        price=price,
        mileage=mileage,
        source_site=sitecode,
        location=location,
        listing_url=url,
        image_url=image_url,
        vin=vin,
    )


def scrape_search(
    search_url: str,
    max_listings: Optional[int] = None,
    headless: bool = True,
    extra_wait_seconds: float = 6.0,
    click_more_rounds: int = 0,
) -> list[Listing]:
    """Scrape one AutoTempest search-results page.

    `extra_wait_seconds` gives the page's per-source async fetches time to
    finish after the first results appear (AutoTempest, Cars.com, Carvana,
    eBay, etc. each load independently). `click_more_rounds` optionally
    clicks each source's "More Results" button that many times to pull in
    additional pages per source (each source paginates independently).
    """
    listings: list[Listing] = []
    seen_urls = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            )
        )
        page.goto(search_url, timeout=45000)
        page.wait_for_selector(SELECTORS["row"], timeout=20000)

        # Let the other sources' async fetches finish loading.
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        page.wait_for_timeout(int(extra_wait_seconds * 1000))

        for _ in range(click_more_rounds):
            more_buttons = page.query_selector_all("button.more-results")
            clicked = False
            for btn in more_buttons:
                try:
                    if btn.is_visible() and not btn.is_disabled():
                        btn.click(timeout=2000)
                        clicked = True
                        page.wait_for_timeout(1500)
                except Exception:
                    continue
            if not clicked:
                break

        rows = page.query_selector_all(SELECTORS["row"])
        for section in rows:
            listing = _extract_row(page, section)
            if listing is None or listing.listing_url in seen_urls:
                continue
            seen_urls.add(listing.listing_url)
            listings.append(listing)
            if max_listings and len(listings) >= max_listings:
                break

        browser.close()

    return listings


if __name__ == "__main__":
    url = build_search_url(make="toyota", model="camry", zip="90001", rad=50)
    results = scrape_search(url, max_listings=20, headless=True)
    for r in results:
        print(r.title, "|", r.price, "|", r.vin, "|", r.source_site, "|", r.listing_url)

"""
Given a listing's outbound URL (the destination dealer/marketplace page that
AutoTempest links to), this module:

  1. Extracts the VIN, where present.
  2. Extracts factory-installed options / equipment, where the source page
     publishes them (many dealer sites do, in a "Features"/"Options"/
     "Equipment" list or in embedded schema.org/Vehicle JSON-LD).
  3. Falls back to NHTSA's free vPIC API to decode the VIN into trim/engine/
     drivetrain/body-style. vPIC does NOT return factory option packages
     (e.g. "Premium Package", "Sport Package") -- only broad specs -- so
     it's a supplement, not a replacement, for step 2.

This deliberately does NOT touch any VIN-history site that gates its report
behind a CAPTCHA. See the conversation for why.
"""
import re
import json
import time
import requests
from bs4 import BeautifulSoup
from typing import Optional

VIN_RE = re.compile(r"\b(?=[A-HJ-NPR-Z0-9]{17}\b)(?!.*[IOQ])[A-HJ-NPR-Z0-9]{17}\b")

# Headers/labels that typically introduce a factory-options list on dealer
# listing pages. Expand this list as you encounter new source sites.
OPTIONS_SECTION_HINTS = [
    "features", "options", "equipment", "vehicle features",
    "factory options", "standard equipment", "optional equipment",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


def fetch_page(url: str, timeout: int = 20) -> Optional[BeautifulSoup]:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser")
    except requests.RequestException:
        return None


def extract_vin(soup: BeautifulSoup, page_text: str) -> Optional[str]:
    """Try structured sources first (reliable), then fall back to regex over text."""
    # 1. schema.org/Vehicle JSON-LD
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        candidates = data if isinstance(data, list) else [data]
        for c in candidates:
            if isinstance(c, dict):
                vin = c.get("vehicleIdentificationNumber") or c.get("vin")
                if vin and VIN_RE.fullmatch(vin.strip()):
                    return vin.strip()

    # 2. Explicit "VIN:" label in visible text
    labeled = re.search(r"VIN[:#]?\s*([A-HJ-NPR-Z0-9]{17})", page_text, re.IGNORECASE)
    if labeled:
        return labeled.group(1)

    # 3. Bare 17-char VIN pattern anywhere in the text (least reliable)
    match = VIN_RE.search(page_text)
    return match.group(0) if match else None


def extract_factory_options(soup: BeautifulSoup) -> list[str]:
    """Best-effort extraction of a factory options/equipment list from the page.

    Looks for a heading matching OPTIONS_SECTION_HINTS, then collects the
    <li> items in the list that follows it. This works on many dealer sites
    but not all -- pages vary too much for one selector to cover everything.
    """
    options: list[str] = []

    for heading in soup.find_all(["h1", "h2", "h3", "h4", "strong", "b", "span"]):
        text = (heading.get_text() or "").strip().lower()
        if not text or not any(hint in text for hint in OPTIONS_SECTION_HINTS):
            continue

        # Look for the next list-like sibling
        list_el = heading.find_next(["ul", "ol"])
        if not list_el:
            continue
        items = [li.get_text(strip=True) for li in list_el.find_all("li")]
        items = [i for i in items if i]
        if items:
            options.extend(items)

    # De-duplicate, keep order
    seen = set()
    deduped = []
    for o in options:
        if o.lower() not in seen:
            seen.add(o.lower())
            deduped.append(o)
    return deduped


NHTSA_DECODE_URL = "https://vpic.nhtsa.dot.gov/api/vehicles/decodevin/{vin}?format=json"

# vPIC uses null / "" / "Not Applicable" for values it has no data on.
_NO_VALUE = {"", "null", "not applicable"}


def decode_vin_nhtsa(vin: str, retries: int = 2) -> dict:
    """Decode a VIN via NHTSA's free vPIC API. Returns broad specs (year, make,
    model, trim, ...), not factory option packages. Any spec vPIC has no data
    for comes back as None.

    Transient failures are retried; if the lookup still fails, a warning is
    printed (so it is never silent) and {} is returned."""
    url = NHTSA_DECODE_URL.format(vin=vin)
    results = None
    last_error = None
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            results = {}
            for item in resp.json().get("Results", []):
                value = str(item.get("Value") or "").strip()
                if value.lower() not in _NO_VALUE:
                    results[item["Variable"]] = value
            break
        except (requests.RequestException, ValueError, KeyError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))

    if results is None:
        print(f"  NHTSA decode failed for {vin}: {last_error.__class__.__name__}: {last_error}")
        return {}

    return {
        "year": results.get("Model Year"),
        "make": results.get("Make"),
        "model": results.get("Model"),
        "trim": results.get("Trim"),
        "engine": results.get("Engine Configuration"),
        "drivetrain": results.get("Drive Type"),
        "body_style": results.get("Body Class"),
    }


def enrich_listing(listing_url: str, known_vin: Optional[str] = None) -> dict:
    """Fetch a listing's destination page and pull factory options (+ VIN
    if not already known from the AutoTempest search page itself)."""
    soup = fetch_page(listing_url)
    if soup is None:
        return {"vin": known_vin, "options": [], "specs": {}}

    page_text = soup.get_text(" ", strip=True)
    vin = known_vin or extract_vin(soup, page_text)
    options = extract_factory_options(soup)
    specs = decode_vin_nhtsa(vin) if vin else {}

    return {"vin": vin, "options": options, "specs": specs}


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        print("Usage: python vin_lookup.py <listing_url>")
        sys.exit(1)
    result = enrich_listing(sys.argv[1])
    print(json.dumps(result, indent=2))

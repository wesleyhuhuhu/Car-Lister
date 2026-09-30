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


NHTSA_BATCH_URL = "https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVINValuesBatch/"
NHTSA_BATCH_MAX = 50   # vPIC accepts at most 50 VINs per batch request


def _flat_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def _specs_from_flat(record: dict) -> dict:
    """One record of the flat batch response -> the same dict decode_vin_nhtsa
    returns. Key spelling is matched loosely ('ModelYear' or 'Model Year')."""
    flat = {}
    for key, value in record.items():
        value = str(value if value is not None else "").strip()
        flat[_flat_key(key)] = None if value.lower() in _NO_VALUE else value
    return {
        "year": flat.get("modelyear"),
        "make": flat.get("make"),
        "model": flat.get("model"),
        "trim": flat.get("trim"),
        "engine": flat.get("engineconfiguration"),
        "drivetrain": flat.get("drivetype"),
        "body_style": flat.get("bodyclass"),
    }


def decode_vins_nhtsa_batch(
    vins: list[str],
    batch_size: int = NHTSA_BATCH_MAX,
    retries: int = 2,
    pause: float = 0.5,
    max_failed_batches: int = 2,
    on_batch=None,
) -> dict:
    """Decode many VINs with vPIC's batch endpoint: one request per 50 VINs
    instead of one per VIN, which keeps us far away from NHTSA's rate limit.

    `on_batch(specs_by_vin)` is called after every successful batch. Stops early
    after `max_failed_batches` failed batches in a row (NHTSA is probably rate
    limiting) so the rest of the run is not wasted. Returns:
        failed        VINs that were not decoded (failed batches + not attempted)
        stopped       True if it gave up early
        unrecognized  True if NHTSA answered but not in the expected shape, so
                      the caller can fall back to one-at-a-time decoding
    Only VINs that NHTSA actually answered for appear in `on_batch`; a VIN it
    answered "no data" for comes back with all values None."""
    vins = list(dict.fromkeys(v.strip().upper() for v in vins if v and v.strip()))
    batch_size = max(1, min(batch_size, NHTSA_BATCH_MAX))
    failed: list[str] = []
    unrecognized = stopped = False
    failed_in_a_row = 0

    for start in range(0, len(vins), batch_size):
        batch = vins[start:start + batch_size]
        specs_by_vin = None
        last_error = None
        for attempt in range(retries + 1):
            try:
                resp = requests.post(
                    NHTSA_BATCH_URL,
                    data={"format": "json", "data": ";".join(batch)},
                    timeout=60,
                )
                resp.raise_for_status()
                records = resp.json().get("Results") or []
                by_vin = {}
                for rec in records:
                    if isinstance(rec, dict):
                        vin = str(rec.get("VIN") or "").strip().upper()
                        if vin:
                            by_vin[vin] = _specs_from_flat(rec)
                if not by_vin and len(records) == len(batch) and all(isinstance(r, dict) for r in records):
                    by_vin = {v: _specs_from_flat(r) for v, r in zip(batch, records)}  # no VIN echoed: match by order
                if not by_vin:
                    raise ValueError("unexpected response shape from NHTSA batch endpoint")
                specs_by_vin = {v: by_vin[v] for v in batch if v in by_vin}
                break
            except ValueError as exc:            # includes bad JSON
                last_error = exc
                unrecognized = True
                break
            except requests.RequestException as exc:
                last_error = exc
                if attempt < retries:
                    time.sleep(3.0 * (attempt + 1))

        if specs_by_vin is None:
            failed.extend(batch)
            failed_in_a_row += 1
            print(f"  NHTSA batch of {len(batch)} failed: {last_error.__class__.__name__}: {last_error}")
            if unrecognized or failed_in_a_row >= max_failed_batches:
                stopped = True
                failed.extend(vins[start + batch_size:])
                break
        else:
            failed_in_a_row = 0
            missing = [v for v in batch if v not in specs_by_vin]
            failed.extend(missing)
            if on_batch:
                on_batch(specs_by_vin)
        if start + batch_size < len(vins):
            time.sleep(pause)

    return {"failed": failed, "stopped": stopped, "unrecognized": unrecognized}


def decode_all(vins: list[str], batch_size: int = NHTSA_BATCH_MAX, on_batch=None) -> list[str]:
    """Decode VINs with the batch endpoint, falling back to one VIN at a time
    (slowly) if NHTSA answers the batch endpoint in a shape we don't understand.
    `on_batch(specs_by_vin)` is called as results arrive. Returns the VINs that
    could not be decoded (NHTSA unreachable or rate limiting)."""
    result = decode_vins_nhtsa_batch(vins, batch_size=batch_size, on_batch=on_batch)
    failed = result["failed"]
    if not (result["unrecognized"] and failed):
        return failed
    print("  Batch decoding not usable; falling back to one VIN at a time...")
    in_a_row, still_failed = 0, []
    for i, vin in enumerate(failed):
        if in_a_row >= 5:
            still_failed = still_failed + failed[i:]
            print("  NHTSA keeps failing; stopping decoding for this run.")
            break
        specs = decode_vin_nhtsa(vin)
        if specs:
            in_a_row = 0
            if on_batch:
                on_batch({vin: specs})
        else:
            in_a_row += 1
            still_failed.append(vin)
        time.sleep(0.3)
    return still_failed


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

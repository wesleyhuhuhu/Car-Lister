"""
Routes a listing to a manufacturer, and optionally to a per-make URL.

The primary method is parsing the manufacturer straight out of the listing
title, since AutoTempest titles are consistently "<year> <make>
<model...>" (e.g. "2022 BMW M3 Competition") -- see get_make_from_title().
This needs no lookup table and has no ambiguity between related brands.

get_make() / WMI_TO_MAKE below are a VIN-prefix (WMI, World Manufacturer
Identifier -- the first 2-3 characters of any VIN, standardized by ISO
3780) fallback for the rare case a title doesn't parse. It's necessarily
incomplete (there are hundreds of WMIs, including many regional/plant-
specific ones per manufacturer, and some WMIs are shared between related
brands like Nissan/Infiniti), so extend the table as needed but prefer the
title parse where you have a title available.

SITE_URL_TEMPLATES is left as a stub -- fill in the site(s) you want to
route each make to, as an f-string-style template containing "{vin}".
"""
import re

VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")

# A few representative WMIs per common make. Many manufacturers use several
# WMIs across different plants/regions -- add more prefixes as you find
# VINs that don't match.
WMI_TO_MAKE = {
    # BMW
    "WBA": "BMW", "WBS": "BMW", "WBX": "BMW", "WBY": "BMW", "4US": "BMW", "5UX": "BMW",
    # Mercedes-Benz
    "WDB": "Mercedes-Benz", "WDC": "Mercedes-Benz", "WDD": "Mercedes-Benz", "4JG": "Mercedes-Benz",
    # Audi
    "WAU": "Audi", "WA1": "Audi", "TRU": "Audi",
    # Volkswagen
    "WVW": "Volkswagen", "3VW": "Volkswagen", "1VW": "Volkswagen", "WV1": "Volkswagen", "WV2": "Volkswagen",
    # Porsche
    "WP0": "Porsche", "WP1": "Porsche",
    # Toyota
    "4T1": "Toyota", "4T3": "Toyota", "5TD": "Toyota", "JTD": "Toyota", "JTE": "Toyota",
    "JTH": "Toyota", "JTJ": "Toyota", "JTK": "Toyota", "JTL": "Toyota", "JTM": "Toyota",
    "JTN": "Toyota", "5YF": "Toyota",
    # Lexus
    "JTH": "Lexus",  # overlaps with Toyota by model year/plant; see note above
    "JT4": "Lexus", "2T2": "Lexus",
    # Honda
    "1HG": "Honda", "2HG": "Honda", "3HG": "Honda", "JHM": "Honda", "5FN": "Honda", "5J6": "Honda",
    # Acura
    "19U": "Acura", "2HN": "Acura", "JH4": "Acura",
    # Nissan
    "1N4": "Nissan", "1N6": "Nissan", "3N1": "Nissan", "JN1": "Nissan", "JN8": "Nissan", "5N1": "Nissan",
    # Infiniti
    "JNK": "Infiniti", "JN1": "Infiniti",  # overlaps with Nissan
    # Ford
    "1FA": "Ford", "1FT": "Ford", "1FM": "Ford", "3FA": "Ford", "NM0": "Ford",
    # Chevrolet
    "1G1": "Chevrolet", "1GC": "Chevrolet", "1GB": "Chevrolet", "3GN": "Chevrolet", "KL8": "Chevrolet",
    # GMC
    "1GT": "GMC", "1GD": "GMC",
    # Dodge / Ram / Chrysler / Jeep (Stellantis)
    "1C3": "Chrysler", "1C4": "Jeep", "1C6": "Ram", "2C3": "Chrysler", "3C4": "Chrysler",
    "1D4": "Dodge", "1D7": "Dodge", "1B3": "Dodge", "1B4": "Dodge",
    # Hyundai
    "KMH": "Hyundai", "5NP": "Hyundai", "KM8": "Hyundai",
    # Kia
    "KND": "Kia", "5XX": "Kia", "5XY": "Kia",
    # Subaru
    "JF1": "Subaru", "JF2": "Subaru", "4S3": "Subaru", "4S4": "Subaru",
    # Mazda
    "JM1": "Mazda", "JM3": "Mazda", "1YV": "Mazda", "4F2": "Mazda",
    # Volvo
    "YV1": "Volvo", "YV4": "Volvo", "7JR": "Volvo",
    # Tesla
    "5YJ": "Tesla", "7SA": "Tesla",
}


def get_make(vin: str) -> str | None:
    """Return the manufacturer for a VIN based on its WMI prefix, or None
    if the VIN's WMI isn't in the table (extend WMI_TO_MAKE as needed --
    the full official WMI list runs into the hundreds of entries)."""
    if not vin or not VIN_RE.match(vin.upper()):
        return None
    vin = vin.upper()
    # Most WMIs are 3 characters; a handful of low-volume manufacturers use
    # a 3rd-character wildcard scheme, but the plain 3-char lookup below
    # covers the vast majority of everyday production vehicles.
    return WMI_TO_MAKE.get(vin[:3])


# Canonical make names as they appear in AutoTempest listing titles (which
# are consistently "<year> <make> <model...>", e.g. "2022 BMW M3
# Competition"). Sorted longest-first below so multi-word makes ("Land
# Rover", "Alfa Romeo") are matched before a shorter make that could be a
# prefix of another word.
COMMON_MAKES = [
    "Mercedes-Benz", "Alfa Romeo", "Land Rover", "Aston Martin", "Rolls-Royce",
    "Acura", "Audi", "BMW", "Buick", "Cadillac", "Chevrolet", "Chrysler",
    "Dodge", "Ferrari", "Fiat", "Ford", "Genesis", "GMC", "Honda", "Hyundai",
    "Infiniti", "Jaguar", "Jeep", "Kia", "Lamborghini", "Lexus", "Lincoln",
    "Lotus", "Maserati", "Mazda", "Mini", "Mitsubishi", "Nissan", "Porsche",
    "Ram", "Subaru", "Tesla", "Toyota", "Volkswagen", "Volvo", "Scion",
    "Pontiac", "Saturn", "Saab", "Suzuki", "Isuzu", "Hummer",
]
_MAKES_BY_LENGTH = sorted(COMMON_MAKES, key=len, reverse=True)
_YEAR_PREFIX_RE = re.compile(r"^\s*(19|20)\d{2}\s+")


def get_make_from_title(title: str) -> str | None:
    """Parse the manufacturer out of an AutoTempest-style listing title,
    which is consistently "<year> <make> <model...>". Returns the make in
    its canonical casing (e.g. "Mercedes-Benz"), or None if the title
    doesn't start with a year or doesn't match a known make.

    This is generally more reliable than a VIN-prefix (WMI) lookup: it
    needs no manufacturer-code table to maintain, and it doesn't run into
    WMI collisions between related brands (e.g. Nissan/Infiniti).
    """
    if not title:
        return None
    rest = _YEAR_PREFIX_RE.sub("", title, count=1)
    if rest == title:
        # No leading year found -- title doesn't match the expected shape.
        return None
    rest_lower = rest.strip().lower()
    for make in _MAKES_BY_LENGTH:
        if rest_lower.startswith(make.lower()):
            return make
    return None


# Fill in the site(s) you want each make routed to. Use "{vin}" as the
# placeholder when the site supports VIN-in-URL lookups. Blank or "..."
# entries are treated as "not configured".
SITE_URL_TEMPLATES: dict[str, str] = {
    "BMW": "https://bimmer.work/",
    # "Toyota": "https://example.com/vin/{vin}",
}


def _template_for(make: str | None) -> str | None:
    template = SITE_URL_TEMPLATES.get(make) if make else None
    return template if template and template.strip(". ") else None


def route_listing(title: str | None = None, vin: str | None = None) -> tuple[str | None, str | None]:
    """Return (make, url) for a listing. Prefers the make parsed from the
    title, falling back to the VIN's WMI prefix. No network or browser
    activity happens here."""
    make = get_make_from_title(title) if title else None
    if make is None and vin:
        make = get_make(vin)
    template = _template_for(make)
    url = template.format(vin=vin) if template and vin else None
    return make, url


def get_option_site_url(vin: str) -> tuple[str | None, str | None]:
    """VIN-only routing (WMI prefix). Side-effect free."""
    return route_listing(vin=vin)


def lookup_factory_options(make: str | None, vin: str) -> dict | None:
    """Return the make-specific build sheet for a VIN (this is the
    'build_sheet'), or None if the make has no lookup or the lookup failed.

    Failures are caught so one bad VIN can't kill a whole run. Do NOT call
    this from a thread pool: each BMW lookup drives one real Chrome window
    on a fixed debugging port, so lookups must run one at a time.
    """
    if make == "BMW":
        from bmw_lookup import lookup_bmw_vin  # lazy: needs Playwright + Chrome
        try:
            return lookup_bmw_vin(vin)
        except Exception as exc:
            print(f"  BMW lookup failed for {vin}: {exc.__class__.__name__}: {exc}")
            return None
    return None


def get_build_sheet(title: str | None, vin: str) -> tuple[str | None, dict | None]:
    """Convenience: route a listing by title/VIN, then fetch its build sheet."""
    make, _url = route_listing(title=title, vin=vin)
    return make, lookup_factory_options(make, vin)


if __name__ == "__main__":
    import json
    import sys

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    do_lookup = "--lookup" in sys.argv  # opens Chrome; off by default
    for test_vin in args or ["WBS8M9C50J5M12345", "4T1BF1FK5HU363898"]:
        make, url = route_listing(vin=test_vin)
        print(f"{test_vin}: make={make!r} url={url!r}")
        if do_lookup:
            sheet = lookup_factory_options(make, test_vin)
            print(json.dumps(sheet, indent=2, ensure_ascii=False) if sheet else "  (no build sheet)")

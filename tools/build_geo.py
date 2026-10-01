"""
Build docs/geo.json: coordinates for US ZIP codes and for "City, ST" names, used by the listings page's
"within N miles of ZIP" filter. Source: the US Census Gazetteer files (public domain).

    python tools/build_geo.py

Listings say where they are as "City, ST" (e.g. "Costa Mesa, CA"), so city names are matched to Census
places (cities, towns, CDPs) and county subdivisions (townships, New England towns). Neighbourhood names that
listing sites use but the Census doesn't (Van Nuys, Valencia, Staten Island...) are mapped in ALIASES.
Coordinates are rounded to 2 decimals (about 1 km), plenty for a radius filter.
The page's lookup key (geoKey() in docs/app.js) must normalise names exactly like key() below.
"""
import io
import json
import re
import urllib.request
import zipfile
from pathlib import Path

YEAR = 2024
BASE = f"https://www2.census.gov/geo/docs/maps-data/data/gazetteer/{YEAR}_Gazetteer/"
OUT = Path(__file__).resolve().parent.parent / "docs" / "geo.json"

SUFFIX = re.compile(
    r"\s+(city and borough|(consolidated|metropolitan|unified|metro) government|city|town|township|village|borough|cdp|"
    r"municipality|comunidad|zona urbana|urban county|plantation|charter township|gore|grant|location|purchase|"
    r"unorganized territory)$", re.I)

# Names listing sites use -> the Census place they belong to (already in clean form).
ALIASES = {
    "CA|Van Nuys": "CA|Los Angeles", "CA|Studio City": "CA|Los Angeles", "CA|Canoga Park": "CA|Los Angeles",
    "CA|North Hollywood": "CA|Los Angeles", "CA|Sherman Oaks": "CA|Los Angeles", "CA|Woodland Hills": "CA|Los Angeles",
    "CA|Encino": "CA|Los Angeles", "CA|Tarzana": "CA|Los Angeles", "CA|Reseda": "CA|Los Angeles", "CA|Northridge": "CA|Los Angeles",
    "CA|Chatsworth": "CA|Los Angeles", "CA|Granada Hills": "CA|Los Angeles", "CA|Sun Valley": "CA|Los Angeles",
    "CA|San Pedro": "CA|Los Angeles", "CA|Wilmington": "CA|Los Angeles", "CA|Harbor City": "CA|Los Angeles",
    "CA|Panorama City": "CA|Los Angeles", "CA|Pacoima": "CA|Los Angeles", "CA|Sylmar": "CA|Los Angeles",
    "CA|Hollywood": "CA|Los Angeles", "CA|North Hills": "CA|Los Angeles", "CA|La": "CA|Los Angeles",
    "CA|Valencia": "CA|Santa Clarita", "CA|Newhall": "CA|Santa Clarita", "CA|Canyon Country": "CA|Santa Clarita",
    "CA|Saugus": "CA|Santa Clarita", "CA|City Of Industry": "CA|Industry", "CA|La Crescenta": "CA|La Crescenta-Montrose",
    "CA|Montrose": "CA|La Crescenta-Montrose", "CA|Paso Robles": "CA|El Paso de Robles (Paso Robles)",
    "CA|Newport Coast": "CA|Newport Beach", "CA|Anaheim Hills": "CA|Anaheim",
    "HI|Honolulu": "HI|Honolulu", "MO|Kcmo": "MO|Kansas City",
    "NY|Long Island City": "NY|New York", "NY|Staten Island": "NY|New York", "NY|Brooklyn": "NY|New York",
    "NY|Bronx": "NY|New York", "NY|Queens": "NY|New York", "NY|Manhattan": "NY|New York", "NY|Jamaica": "NY|New York",
    "NY|Flushing": "NY|New York", "NY|Richmond Hill": "NY|New York", "NY|Bayside": "NY|New York",
    "TN|Nashville": "TN|Nashville-Davidson", "KY|Louisville": "KY|Louisville/Jefferson County",
    "GA|Augusta": "GA|Augusta-Richmond County", "GA|Athens": "GA|Athens-Clarke County", "MT|Butte": "MT|Butte-Silver Bow",
    "GA|Macon": "GA|Macon-Bibb County", "KY|Lexington": "KY|Lexington-Fayette", "ID|Boise": "ID|Boise City",
    "MA|Hyannis": "MA|Barnstable Town", "MA|Westboro": "MA|Westborough", "FL|Lake Worth": "FL|Lake Worth Beach",
}


def key(state: str, city: str) -> str:
    c = city.lower().strip()
    c = re.sub(r"\btownship\b|\btwp\.?", "", c)
    c = c.replace("saint ", "st ").replace("sainte ", "ste ").replace("mount ", "mt ").replace("fort ", "ft ")
    return state.upper() + "|" + re.sub(r"[^a-z0-9]", "", c)


def clean(name: str) -> str:
    name = re.sub(r"\s*\(balance\)$", "", name.strip(), flags=re.I)      # "Indianapolis city (balance)"
    return re.sub(r"^urban\s+", "", SUFFIX.sub("", name).strip(), flags=re.I)   # ONE suffix: "Redwood City city" -> "Redwood City"


def rows(name: str):
    print(f"Downloading {name} ...")
    data = urllib.request.urlopen(BASE + f"{YEAR}_Gaz_{name}_national.zip", timeout=120).read()
    z = zipfile.ZipFile(io.BytesIO(data))
    lines = z.read(z.namelist()[0]).decode("latin-1").splitlines()
    head = [h.strip() for h in lines[0].split("\t")]
    for line in lines[1:]:
        yield dict(zip(head, (c.strip() for c in line.split("\t"))))


def coords(r: dict) -> list:
    return [round(float(r["INTPTLAT"]), 2), round(float(r["INTPTLONG"]), 2)]


def main():
    zips = {r["GEOID"]: coords(r) for r in rows("zcta")}
    places: dict[str, list] = {}
    for r in rows("place"):
        k = key(r["USPS"], clean(r["NAME"]))
        if k not in places or r["LSAD"] != "57":        # an incorporated place wins over a CDP (LSAD 57) of the same name
            places[k] = coords(r)
    for r in rows("cousubs"):                            # townships / New England towns, only where no place exists
        places.setdefault(key(r["USPS"], clean(r["NAME"])), coords(r))
    for alias, target in ALIASES.items():
        (sa, ca), (sb, cb) = alias.split("|"), target.split("|")
        if key(sb, cb) in places:
            places.setdefault(key(sa, ca), places[key(sb, cb)])
        else:
            print(f"  alias target not found: {target}")
    OUT.write_text(json.dumps({"zips": zips, "places": places}, separators=(",", ":")), encoding="utf-8")
    print(f"Wrote {OUT} ({len(zips)} ZIPs, {len(places)} places, {OUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()

# AutoTempest Factory-Options Finder

Scrapes AutoTempest search results, follows each listing to its source page
to pull the VIN and any published factory options, and lets you filter
results by those options.

## Setup

```bash
pip install -r requirements.txt
playwright install chromium
```

## Run

```bash
python main.py --make toyota --model camry --zip 90001 --rad 50 \
    --minyear 2019 --maxyear 2023 --max-listings 30 \
    --require "sunroof,leather" --delay 1.5
```

Outputs `listings.csv` and `listings.json`.

## How it works

1. **`autotempest_scraper.py`** — drives a headless Chromium browser with
   Playwright to load an AutoTempest search-results page. Each result card
   (`li.result-list-item section.search-result`) is scraped for title,
   price, mileage, location/dealer, image, the outbound link to the
   original listing, and — conveniently — the **VIN**, which AutoTempest
   embeds directly in each card's "Share" button (`onclick="...shareListing(\`cs\`,
   \`VIN\`, ...)"`). That means VINs come straight off the search page with
   no extra requests. AutoTempest loads each source (Cars.com, Carvana,
   eBay, CarSoup, Hemmings, "Other") via separate async fetches after the
   page loads, so the scraper waits for network idle plus a short buffer
   before reading the DOM.
2. **`vin_lookup.py`** — for each outbound link, fetches the destination
   page and tries to pull a **factory options/equipment list**, if the
   source page publishes one (common on dealer sites, under a
   "Features"/"Options"/"Equipment" heading, or in Carvana's bullet-style
   details). If a VIN wasn't already found on the search page, it also
   falls back to `schema.org/Vehicle` JSON-LD, a labeled "VIN:" field, or a
   bare VIN-pattern regex. It calls NHTSA's free **vPIC** API to decode the
   VIN into broad specs (trim, engine, drivetrain, body style) as a
   supplement — vPIC does *not* return factory option packages, only specs.
3. **`main.py`** — orchestrates the above, filters listings down to ones
   whose extracted options match all your `--require` keywords, and saves
   CSV/JSON.

## Important limitations (please read)

- **Selectors will drift.** AutoTempest's HTML/class names can change
  without notice. If `scrape_search` returns nothing, re-inspect the live
  page in your browser's dev tools and update `SELECTORS` in
  `autotempest_scraper.py`.
- **Not every source site publishes a factory-options list.** Coverage
  depends entirely on what the dealer/marketplace page includes. You'll get
  strong hit rates on dealer sites with detailed listings, and little to
  nothing from bare-bones private-party posts.
- **No VIN-history/build-sheet site is scraped.** Sites that gate detailed
  factory build data behind a CAPTCHA are deliberately excluded — automating
  past that kind of anti-bot control isn't something this tool does. If you
  need more complete factory-option data than what listing pages publish,
  the legitimate path is a paid NMVTIS-approved API (e.g. VinAudit,
  ClearVin) used with a real API key — that's a drop-in replacement for the
  `decode_vin_nhtsa` call in `vin_lookup.py`.
- **Be a reasonable citizen of the sites you hit.** `--delay` throttles
  requests to destination pages; keep `--max-listings` sane; and check each
  site's terms of service before scraping at scale.
- **Check AutoTempest's own Terms of Service** before scraping it — as an
  aggregator, its ToS may restrict automated use of its results.

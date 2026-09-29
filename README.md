# AutoTempest Car Lister

Two separate steps, so the slow, rate-limited option lookup only runs when you ask for it.

## Setup

```bash
pip install -r requirements.txt
playwright install chromium
```

`bmw_lookup.py` drives your own Chrome; check `CHROME_PATH` / `CHROME_PROFILE` at the top of it.

## 1. Scrape listings

```bash
python main.py --make bmw --model m3 --zip 91748 --radius 500 --minyear 2021
```

Saves `listings.json` and `listings.csv`: title, price, mileage, location, source site, link, VIN,
plus year/make/model/trim from NHTSA. Re-running refreshes price/mileage for known VINs and appends
new ones. Radius accepts miles, or `state` / `country` (nationwide) / `any`.
Factory options are not collected here.

## 2. Look up factory options for specific VINs

```bash
python lookup_options.py WBS23HJ03VFX24999
python lookup_options.py VIN1 VIN2 VIN3 --delay 15
```

Replaces the `options` column for those rows (`"248 Steering Wheel Heating"`, ...) in the JSON and CSV
and stores the full build sheet (market, color, upholstery, ...) under `build_sheet` in the JSON.
A looked-up VIN is skipped next time (`--force` re-checks) and is never overwritten by `main.py`.
Saves after every VIN; stops after 3 failures in a row. BMW only for now (bimmer.work).

Rows saved before this change still hold old scraped options, which were unreliable. They stay until you look that VIN up.

## Debug helpers

- `debug_selectors.py`: dumps the AutoTempest page to fix selectors if the site changes.
- `debug_bmw_page.py <VIN>`: dumps the bimmer.work vehicle and options pages.

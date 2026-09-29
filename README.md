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

## 2b. Look up every matching VIN (with rate-limit handling)

```bash
python lookup_matching.py --dry-run          # just list what would be looked up
python lookup_matching.py                    # year >= 2022, trim "M3 xDrive Competition"
python lookup_matching.py --limit 5          # only try 5 this run
python lookup_matching.py --cooldown 30      # if bimmer.work stops, wait 30 min and continue
```

Options: `--min-year`, `--trim`, `--delay` (seconds between lookups, default 10), `--max-failures` (default 3),
`--max-cooldowns` (default 3). Progress is saved after every VIN. When bimmer.work stops responding it
stops cleanly and reports how many worked; run it again later and finished VINs are skipped.

## 3. Put the listings in an online database

Hosted PostgreSQL (Supabase or Neon, both have free tiers). Factory options are stored as a list of codes, so "has 248 and 5AU" is one fast query.

1. Create a project and copy its connection string (Supabase: Project Settings > Database > Connection string, "Session pooler").
2. Create a file named `.env` next to the scripts (it is git-ignored):
   `DATABASE_URL=postgresql://user:password@host:5432/postgres`
3. `pip install "psycopg[binary]"`
4. `python db_sync.py --init` the first time, then just `python db_sync.py` after each scrape/lookup.
   `--dry-run` shows what would be sent without connecting.

`listings.json` stays the source of truth. Each sync refreshes price/mileage and never erases a saved options lookup.
Example query (SQL editor in the dashboard):

```sql
select year, trim, price, mileage, listing_url from listings
where option_codes @> array['248','5AU'] and price < 80000 order by price;
```

Row level security is on with no public policy, so only your password can read or write. See the end of `schema.sql` to allow public read.

### Automatic database updates

Once `.env` has `SUPABASE_URL` and `SUPABASE_SERVICE_KEY`, the database updates itself:

- `main.py` pushes every listing after each scrape (changed price/mileage/etc. update the existing row, new VINs are added).
- `lookup_options.py` and `lookup_matching.py` push each VIN's options as soon as it is looked up.
- Add `--no-db` to any of them to skip it. With no `.env` keys, pushing is simply off.
- If the database can't be reached, the local files are still saved, a warning is printed, and pushing pauses for that run.
  Run `python db_sync_https.py` afterwards to catch the database up.

### If your network blocks the database ports

Use `db_sync_https.py` instead (works over normal HTTPS, no psycopg needed). Paste `schema.sql` into the Supabase
SQL Editor and run it once, put `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` (the secret / service_role key) in `.env`,
then `python db_sync_https.py`.

## Debug helpers

- `debug_selectors.py`: dumps the AutoTempest page to fix selectors if the site changes.
- `debug_bmw_page.py <VIN>`: dumps the bimmer.work vehicle and options pages.

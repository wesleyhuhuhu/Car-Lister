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
plus year/make/model/trim from NHTSA (decoded 50 VINs per request, so a big search is only a few requests).
A value already saved is never blanked: if NHTSA is down or rate limiting, existing data is kept and the missing
VINs are simply decoded on the next run. Re-running refreshes price/mileage for known VINs and appends
new ones. Radius accepts miles, or `state` / `country` (nationwide) / `any`.
Factory options are not collected here.

### Fill in missing data on saved rows

```bash
python fill_missing.py --dry-run     # which saved rows are missing year/make/model
python fill_missing.py               # decode them (NHTSA, 50 VINs per request), save, update the database
```

`main.py` only ever decodes the listings its own search returns, so one search never touches another search's rows.
`fill_missing.py` is the one script that goes through everything on file (any make/model): sold listings, VINs that
failed to decode earlier, rows from older searches. It never overwrites a saved value, skips rows with no VIN, and
only pushes the rows it changed to the database. Use `--no-db` to skip the push.

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

## 4. Shared database for other users

Other people ("users") can help look up options; each person's own network has its own bimmer.work limit, so more users means more VINs done.

**One-time setup (owner):**
1. Supabase > SQL Editor: run `schema_shared.sql` (after `schema.sql`). Run it again after updating the project; it is safe to repeat.
2. Supabase > Project Settings > API Keys: copy the Project URL and the **anon / publishable** key.
3. Give users those two values (privately). **Never give out the secret / service_role key.**

**Each user** installs Python, Chrome and `pip install requests playwright truststore`, gets the project files, and creates a `.env`:
```
SUPABASE_URL=https://xxxxxxxx.supabase.co
SUPABASE_ANON_KEY=the-public-anon-key
```

**What users can and can't do:** read the `public_listings` view (this makes the listings public), claim VINs, submit an options result
for a VIN that has none, and hand claims back. They can't read or change the table directly, delete anything, or overwrite an
existing result. Trust still matters: a user could submit wrong options for a VIN that has none yet. To redo a VIN, set its
`build_sheet` to null in the Table Editor. If a key leaks, rotate it in Project Settings > API Keys.

### 4a. Work through the queue: `lookup_shared.py`

```bash
python lookup_shared.py --dry-run                 # how many are waiting / being worked on / done
python lookup_shared.py --cooldown 15 --max-cooldowns 10
```
Options: `--limit`, `--delay`, `--claim-size`, `--min-year`, `--trim`, `--make`, `--worker` (your name in the database).
A claimed VIN is skipped by others for 30 minutes. If bimmer.work stops, unfinished VINs are handed back. If sending a result fails it is kept in
`unsent_options.jsonl` and sent next run.

### 4b. The "Fetch options" button: `companion.py`

A web page can't open Chrome on your computer, so the listings page's button talks to this small app running on the user's own computer:
```bash
python companion.py --allow-origin https://your-listings-page.example
```
It listens only on `127.0.0.1:8765`, only accepts requests from the page addresses you list (`--allow-origin`, or `COMPANION_ORIGINS` in `.env`,
comma separated), does one lookup at a time with a pause between them, and pauses itself for 15 minutes after 3 failures in a row.
Chrome opens on the first click and closes after 5 idle minutes.

For the page: `GET /status`, `POST /fetch {"vin": "..."}` (202 queued, 409 unavailable, 429 blocked, 400 bad VIN, 403 page not allowed),
`GET /job?vin=...` (`queued` / `running` / `done` / `failed`, plus `message`). Read listings from the `public_listings` view
(`GET {SUPABASE_URL}/rest/v1/public_listings` with the anon key); `being_fetched` is true while someone holds a claim, and `options` fills in when a fetch finishes.

## 5. Helper browser extension: search and fetch options from the website

`extension/` is a Chrome/Edge/Brave extension (Manifest V3) that lets the listings page do two things from the visitor's own browser, with no Python:

- **Search AutoTempest** (the form at the top of the page). The extension opens the results in a background tab, loads every source, reads the cards and decodes the VINs with NHTSA. Results are kept in that browser's `localStorage`, not in the shared database.
- **Fetch options** for non-BMW cars. The extension calls the manufacturer's site directly (extensions are not subject to CORS, a plain web page is), so there is no Chrome automation involved.

Install (once per browser): `chrome://extensions` > Developer mode > Load unpacked > pick the `extension` folder, then reload the page; the "Extension connected" badge turns green.
To use it on another site address, add that address to `content_scripts.matches` in `extension/manifest.json`.

| Make | Source | How |
|---|---|---|
| Chrysler, Dodge, Jeep, Ram, Fiat, Alfa Romeo | original window sticker, `/hostd/windowsticker/getWindowStickerPdf.do?vin=` on the brand sites | plain HTTP, PDF text read with pdf.js (`extension/adapters/stellantis*.js`) |
| Mercedes-Benz | not set up yet | `extension/adapters/mercedes.js` |
| Toyota | not set up yet | `extension/adapters/toyota.js` |
| BMW | bimmer.work / oemnavigations.com | still the Python companion (section 4b) |

Adding a make: implement `lookup(vin, make)` in its adapter (contract in `mercedes.js`), set `ready: true` and add the site's address to `host_permissions`. Test the Stellantis parser with `cd extension && npm i && npm test`.

## Debug helpers

- `debug_selectors.py`: dumps the AutoTempest page to fix selectors if the site changes.
- `debug_bmw_page.py <VIN>`: dumps the bimmer.work vehicle and options pages.

## Headless (no visible Chrome window)

No environment variable needed:

- `python companion.py` and `python lookup_shared.py` run **headless by default** (built for other users). Add `--show-browser` to see the window.
- `python lookup_matching.py` and `python lookup_options.py` show the window by default. Add `--headless` to hide it.
- `python check_bimmer.py --headless` works too.

`BMW_HEADLESS=1` still works as a fallback default for the scripts that show the window. If bimmer.work starts behaving differently when headless (or shows a human check, which can't be clicked in a hidden window), run once with `--show-browser`.

## Fallback site: oemnavigations.com

If bimmer.work blocks a lookup (HTTP 429, or no VIN box / Submit button), the lookup automatically tries https://oemnavigations.com/pages/vin-decoder-app in the same Chrome window. Nothing else changes: the result is stored in the same shape (`Options` codes like `248`, plus `Color`, `Upholstery`, `Start of Production`), and `build_sheet["Source"]` says which site answered.

- The site allows **2 free checks per day**. This tool records the attempts in `~/.car-lister/oem_usage.json` (UTC day) as a log only; it never blocks a lookup. The site enforces the limit itself, and when it refuses, its message is shown and the lookup fails.
- The **second check of the day shows a Cloudflare human check**. It is never bypassed: if Chrome is hidden, it is closed and reopened with a visible window so you can tick the box (you get 2 minutes), then hidden again. The lookup is repeated after reopening; the check appears before the VIN is accepted, so it doesn't cost an extra free check.
- After bimmer.work blocks, it is skipped for 10 minutes (`BMW_RETRY_MINUTES`) so each VIN doesn't wait on it first.
- A VIN that bimmer.work simply doesn't know, or a timeout after submitting, does **not** trigger the fallback (it would waste one of the 2 checks).
- Turn it off with `--no-fallback` (all lookup scripts) or `OEM_FALLBACK=0`.
- This site lists options with a leading zero (`0248`, `01CB`); they are converted to bimmer.work's style (`248`, `1CB`). Option wording differs slightly between the sites.

## Contributing scraped listings without the secret key

Anyone with just `SUPABASE_URL` and `SUPABASE_ANON_KEY` in `.env` can run `python main.py ...` and their results are uploaded too (run the updated `schema_shared.sql` in Supabase first; it adds `submit_listings`).

- **New listings** are added, with no overall limit (the scripts send 200 per call; the database accepts up to 500 per call).
- **Listings already in the database** only get their price, mileage, location, link and photo refreshed, and empty year/make/model/trim filled in. Options and build sheets are never changed by this, and nothing can be deleted.
- Options found by `lookup_matching.py` / `lookup_options.py` are submitted too; the first result for a VIN wins.
- The owner (with `SUPABASE_SERVICE_KEY`) still uploads directly, as before.



https://github.com/user-attachments/assets/93a594d0-1744-4c81-9220-b589e738dd90


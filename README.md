# Listings page (GitHub Pages)

A static site: `index.html`, `style.css`, `app.js`, `config.js`. No build step, no dependencies.

## Set up
1. Run `schema_shared.sql` in the Supabase SQL Editor (creates the `public_listings` view the page reads).
2. Edit `config.js`: your `SUPABASE_URL` and the **anon / publishable** key. That key is meant to be public. Never use the secret / service_role key here.
3. Put this `docs/` folder in your GitHub repo, then Settings -> Pages -> "Deploy from a branch" -> `main` / `/docs`.
4. The site appears at `https://<you>.github.io/<repo>/`.

## How "Fetch options" works
The page cannot run scripts on anyone's computer. Instead, a visitor runs `python companion.py --allow-origin https://<you>.github.io` on their own machine. The page talks to it at `http://127.0.0.1:8765`; it does the bimmer.work lookup in that visitor's Chrome and sends the result to the database; the page then reloads that card. If the companion isn't running, the button shows setup instructions. Works in Chrome, Edge and Firefox (Safari blocks it).

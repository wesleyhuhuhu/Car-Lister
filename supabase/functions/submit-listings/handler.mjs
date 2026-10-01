// Logic of the submit-listings Edge Function. Plain JS with injected dependencies so Node can test it
// (handler.test.mjs) and Deno runs it unchanged (index.ts).
//
// What it guarantees before anything reaches the database:
//   * every row has a real VIN: right characters, a correct check digit, and NHTSA's vPIC decodes it;
//   * year / make / model / trim are taken from that decode, never from the caller;
//   * the listing title can't contradict the decoded model year (off by more than 1);
//   * the listing link points at a known listing site, the photo at AutoTempest's thumbnail host;
//   * nothing is written if vPIC can't be reached (no unverified rows slip in).
// The write itself goes through the existing submit_listings SQL function (insert new rows, refresh
// price/mileage/etc. on known ones, never touch options), called with the service key.

export const MAX_ROWS = 500;
export const MAX_BODY_BYTES = 2_000_000;
const VPIC_URL = "https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVINValuesBatch/";

// Listing links are suffix-matched ("www.cars.com" matches "cars.com"). Add more with the
// ALLOWED_LISTING_HOSTS environment variable (comma separated).
export const LISTING_HOSTS = [
  "autotempest.com", "cargurus.com", "cargurus.ca", "cars.com", "carvana.com", "ebay.com", "truecar.com",
  "autotrader.com", "autotrader.ca", "carsoup.com", "hemmings.com", "carsandbids.com", "privateauto.com",
  "autobytel.com", "carsdirect.com", "craigslist.org", "kijiji.ca", "facebook.com", "carmax.com", "vroom.com",
  "shift.com", "cargurus.co.uk", "bringatrailer.com", "classiccars.com", "autolist.com", "edmunds.com",
];
export const IMAGE_HOSTS = ["autotempest.com"];

const CORS = {
  "Access-Control-Allow-Origin": "*",           // no secrets or logins here; validation is what protects the table
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Access-Control-Allow-Headers": "content-type, authorization, apikey, x-client-info",
  "Access-Control-Max-Age": "600",
};
const json = (status, body) => new Response(JSON.stringify(body), { status, headers: { ...CORS, "Content-Type": "application/json" } });

// ---------------------------------------------------------------- pure validation
const TRANSLIT = { A: 1, B: 2, C: 3, D: 4, E: 5, F: 6, G: 7, H: 8, J: 1, K: 2, L: 3, M: 4, N: 5, P: 7, R: 9, S: 2, T: 3, U: 4, V: 5, W: 6, X: 7, Y: 8, Z: 9 };
const WEIGHTS = [8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2];

export function vinCheckDigitOk(vin) {
  let sum = 0;
  for (let i = 0; i < 17; i++) {
    const c = vin[i];
    const v = /\d/.test(c) ? Number(c) : TRANSLIT[c];
    if (v === undefined) return false;
    sum += v * WEIGHTS[i];
  }
  const check = sum % 11;
  return vin[8] === (check === 10 ? "X" : String(check));
}

export function hostAllowed(url, extra = []) {
  let u;
  try { u = new URL(url); } catch { return false; }
  if (u.protocol !== "https:" && u.protocol !== "http:") return false;
  const host = u.hostname.toLowerCase();
  return [...LISTING_HOSTS, ...extra].some((h) => host === h || host.endsWith("." + h));
}

const imageAllowed = (url) => {
  try {
    const u = new URL(url);
    return u.protocol === "https:" && IMAGE_HOSTS.some((h) => u.hostname === h || u.hostname.endsWith("." + h));
  } catch { return false; }
};

const text = (v, max) => { const s = String(v ?? "").replace(/[\u0000-\u001f]/g, " ").trim().slice(0, max); return s || null; };
const intIn = (v, lo, hi) => { const n = Number(v); return v != null && v !== "" && Number.isInteger(n) && n >= lo && n <= hi ? n : null; };

// One raw row from the caller -> a clean row, or { reject: reason }.
export function cleanRow(raw, extraHosts = []) {
  if (!raw || typeof raw !== "object") return { reject: "not_an_object" };
  const vin = String(raw.vin ?? "").trim().toUpperCase();
  if (!/^[A-HJ-NPR-Z0-9]{17}$/.test(vin)) return { reject: "bad_vin" };
  if (!vinCheckDigitOk(vin)) return { reject: "bad_check_digit", vin };
  const title = text(raw.title, 300);
  if (!title) return { reject: "no_title", vin };
  const url = text(raw.listing_url, 1000);
  if (!url || !hostAllowed(url, extraHosts)) {
    let host = ""; try { host = new URL(url).hostname; } catch { /* not a URL */ }
    return { reject: "listing_url_not_allowed" + (host ? `:${host}` : ""), vin };
  }
  const image = text(raw.image_url, 1000);
  return {
    vin, title, listing_url: url,
    image_url: image && imageAllowed(image) ? image : null,
    price: intIn(raw.price, 1, 5_000_000), price_text: text(raw.price_text, 60),
    mileage: intIn(raw.mileage, 0, 2_000_000), mileage_text: text(raw.mileage_text, 60),
    source_site: text(raw.source_site, 100), location: text(raw.location, 200),
  };
}

const NO_VALUE = new Set(["", "null", "not applicable"]);
const field = (rec, key) => { const v = String(rec?.[key] ?? "").trim(); return NO_VALUE.has(v.toLowerCase()) ? null : v; };

// vPIC record -> specs, or null when vPIC doesn't know this VIN as a vehicle.
export function specsFromVpic(rec) {
  const year = intIn(field(rec, "ModelYear"), 1950, 2100);
  const make = field(rec, "Make");
  if (!year || !make) return null;
  return { year, make: make.slice(0, 60), model: (field(rec, "Model") || "").slice(0, 100) || null, trim: (field(rec, "Trim") || "").slice(0, 100) || null };
}

// Merge decoded specs into a cleaned row; reject when the title's year contradicts the VIN.
export function withSpecs(row, specs) {
  const titleYear = /^\s*((?:19|20)\d\d)\b/.exec(row.title);
  if (titleYear && Math.abs(Number(titleYear[1]) - specs.year) > 1) return { reject: "title_year_mismatch", vin: row.vin };
  return { ...row, ...specs };
}

// ---------------------------------------------------------------- the request handler
export async function decodeBatch(vins, doFetch) {
  const out = new Map();
  for (let i = 0; i < vins.length; i += 50) {
    const batch = vins.slice(i, i + 50);
    const res = await doFetch(VPIC_URL, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ format: "json", data: batch.join(";") }).toString(),
      signal: AbortSignal.timeout(20_000),
    });
    if (!res.ok) throw new Error(`vPIC answered HTTP ${res.status}`);
    const records = (await res.json()).Results;
    if (!Array.isArray(records)) throw new Error("vPIC answered in an unexpected shape");
    for (const rec of records) {
      const vin = String(rec?.VIN ?? "").toUpperCase();
      if (vin) out.set(vin, specsFromVpic(rec));
    }
  }
  return out;
}

// deps: { fetch, supabaseUrl, serviceKey, extraHosts }
export async function handle(req, deps) {
  if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS });
  if (req.method !== "POST") return json(405, { error: "POST a JSON body like {\"listings\": [...]}" });

  const raw = await req.text();
  if (raw.length > MAX_BODY_BYTES) return json(413, { error: "request too large" });
  let body;
  try { body = JSON.parse(raw); } catch { return json(400, { error: "body is not valid JSON" }); }
  const rows = body && body.listings;
  if (!Array.isArray(rows) || rows.length === 0) return json(400, { error: "send {\"listings\": [ ... ]} with at least one listing" });
  if (rows.length > MAX_ROWS) return json(400, { error: `send at most ${MAX_ROWS} listings per call` });

  const rejected = [];
  const clean = new Map();                                    // one row per VIN (the last copy wins)
  for (const r of rows) {
    const c = cleanRow(r, deps.extraHosts || []);
    if (c.reject) rejected.push({ vin: c.vin || null, reason: c.reject });
    else clean.set(c.vin, c);
  }

  let specs;
  try {
    specs = await decodeBatch([...clean.keys()], deps.fetch);
  } catch (e) {
    return json(503, { error: `Could not verify the VINs with NHTSA (${e.message}). Nothing was saved; try again shortly.` });
  }

  const good = [];
  for (const [vin, row] of clean) {
    const s = specs.get(vin);
    if (!s) { rejected.push({ vin, reason: "vin_not_recognized" }); continue; }
    const merged = withSpecs(row, s);
    if (merged.reject) rejected.push({ vin, reason: merged.reject });
    else good.push(merged);
  }

  let result = { inserted: 0, updated: 0, skipped: 0 };
  if (good.length) {
    const res = await deps.fetch(`${deps.supabaseUrl}/rest/v1/rpc/submit_listings`, {
      method: "POST",
      headers: { apikey: deps.serviceKey, Authorization: `Bearer ${deps.serviceKey}`, "Content-Type": "application/json" },
      body: JSON.stringify({ p_rows: good }),
    });
    if (!res.ok) return json(502, { error: `The database refused the listings (HTTP ${res.status}): ${(await res.text()).slice(0, 300)}` });
    const out = await res.json();
    result = (Array.isArray(out) ? out[0] : out) || result;
  }
  return json(200, {
    inserted: Number(result.inserted) || 0, updated: Number(result.updated) || 0, skipped: Number(result.skipped) || 0,
    rejected_count: rejected.length, rejected: rejected.slice(0, 20),
  });
}

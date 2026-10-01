// Sends searched listings to the shared database through the submit-listings Edge Function, which checks
// every VIN (check digit + NHTSA decode) and takes year/make/model/trim from that decode.
import { SUBMIT_LISTINGS_URL } from "./config.js";

const CHUNK = 200;
const FIELDS = ["vin", "title", "price", "price_text", "mileage", "mileage_text", "source_site", "location", "listing_url", "image_url"];

// Resolves to { inserted, updated, rejected, error } and never throws: a failed upload must not lose the search.
export async function uploadListings(listings, onProgress = () => {}) {
  const rows = listings.filter((l) => l.vin).map((l) => Object.fromEntries(FIELDS.map((f) => [f, l[f] ?? null])));
  const total = { inserted: 0, updated: 0, rejected: 0, reasons: {}, error: null };
  for (let i = 0; i < rows.length; i += CHUNK) {
    onProgress(`Saving to the database… ${Math.min(i + CHUNK, rows.length)}/${rows.length}`);
    try {
      const res = await fetch(SUBMIT_LISTINGS_URL, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ listings: rows.slice(i, i + CHUNK) }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) { total.error = body.error || `The database answered HTTP ${res.status}.`; break; }
      total.inserted += body.inserted || 0;
      total.updated += body.updated || 0;
      total.rejected += body.rejected_count || 0;
      for (const r of body.rejected || []) total.reasons[r.reason] = (total.reasons[r.reason] || 0) + 1;
    } catch (e) {
      total.error = `Could not reach the database (${e.message}).`;
      break;
    }
  }
  return total;
}

// Sends one fetched build sheet. `listing` (the card's fields) lets the function add the car first if it
// isn't in the database yet. Resolves to { stored, reason, error } and never throws.
export async function uploadOptions(vin, listing, result) {
  const row = listing ? Object.fromEntries(FIELDS.map((f) => [f, listing[f] ?? null])) : undefined;
  try {
    const res = await fetch(SUBMIT_LISTINGS_URL, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ options: { vin, listing: row, build_sheet: result.build_sheet, options: result.options, option_codes: result.option_codes } }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) return { stored: false, error: body.error || `The database answered HTTP ${res.status}.` };
    return { stored: !!body.stored, reason: body.reason || null };
  } catch (e) {
    return { stored: false, error: `Could not reach the database (${e.message}).` };
  }
}

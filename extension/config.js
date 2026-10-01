// The shared database's public address. Same project as docs/config.js. The submit-listings Edge Function
// is public by design (it verifies every VIN itself), so no key is needed here.
export const SUPABASE_URL = "https://grhpcdbwnczekgfnsumo.supabase.co";
export const SUBMIT_LISTINGS_URL = `${SUPABASE_URL}/functions/v1/submit-listings`;

// bimmer.work lookups open a tab. true = a visible tab that takes focus, false = a background tab in the
// current window. (Chosen by testing which one bimmer.work answers reliably; see README.)
export const BMW_TAB_ACTIVE = false;

// When bimmer.work blocks (429 / no form), fall back to oemnavigations.com (about 2 free checks a day,
// opens a visible tab; the person ticks its human check if one appears). false = never use it.
export const OEM_FALLBACK = true;

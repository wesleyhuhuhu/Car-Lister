// The shared database's public address. Same project as docs/config.js. The submit-listings Edge Function
// is public by design (it verifies every VIN itself), so no key is needed here.
export const SUPABASE_URL = "https://grhpcdbwnczekgfnsumo.supabase.co";
export const SUBMIT_LISTINGS_URL = `${SUPABASE_URL}/functions/v1/submit-listings`;

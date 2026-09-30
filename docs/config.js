// Settings for the listings page. The "anon" (publishable) key is meant to be public:
// it can only read the public_listings view and call the few database functions
// in schema_shared.sql. NEVER put the service_role / secret key here.
export const CONFIG = {
  SUPABASE_URL: "https://grhpcdbwnczekgfnsumo.supabase.co",
  SUPABASE_ANON_KEY: "sb_publishable_1IYS8llt7KUrvvhDm8ufpA_oIhkCCaY",
  REPO_URL: "https://github.com/wesleyhuhuhu/Car-Lister",
  COMPANION_URL: "http://127.0.0.1:8765",
  SITE_TITLE: "Wesley's Amazing Car Site With OPTIONS",
};

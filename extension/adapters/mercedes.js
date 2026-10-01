// Mercedes-Benz: site not chosen yet. Fill in `lookup` and set ready: true.
// Contract: lookup(vin, make) resolves to { sheet, options }, where
//   sheet   = { Options: { code: text }, Details: {...}, Source: "site name" }  (same shape as the BMW build sheet)
//   options = ["SA code text", ...]  (strings shown on the listing card)
//   codes   = ["SA code", ...]  (optional; only when each option string starts with its code, so the card bolds it)
// and throws an Error with a message fit to show the user when it can't answer.
// Also add the site to "host_permissions" in manifest.json.
export const mercedes = {
  id: "mercedes",
  label: "Mercedes-Benz build sheet",
  makes: ["mercedes-benz", "mercedes", "mercedes-amg"],
  wmi: ["WDB", "WDC", "WDD", "WDF", "W1K", "W1N", "W1V", "4JG", "55S", "W1W"],
  ready: false,
  async lookup() { throw new Error("The Mercedes-Benz lookup site has not been configured yet."); },
};

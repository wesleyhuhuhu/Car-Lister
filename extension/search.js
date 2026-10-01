// AutoTempest search, run in the user's own browser: open the results page in a background tab, wait for
// the per-source results to load, click each "More results" button, read the cards, close the tab.
// The selectors mirror autotempest_scraper.py; if AutoTempest changes its markup, fix both.
const ROW = "li.result-list-item section.search-result";
const LOCALIZATION = new Set(["state", "country", "nationwide", "any"]);
const SITE_NAMES = {
  te: "AutoTempest", hem: "Hemmings", hemc: "Hemmings", cs: "CarSoup", cv: "Carvana", cm: "Cars.com", cmf: "Cars.com",
  cmp: "Cars.com", eb: "eBay", ebcom: "eBay", ot: "Other", at: "AutoTrader.com", ct: "AutoTrader.ca", cg: "CarGurus",
  cgu: "CarGurus", cgc: "CarGurus.ca", kj: "Kijiji.ca", st: "craigslist", fbm: "Facebook Marketplace", abt: "AutoByTel",
  abtc: "AutoByTel", tc: "TrueCar", vast: "VAST", vastc: "VAST", dt: "DealerTrack", cd: "CarsDirect",
};

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export function cleanParams(p) {
  const text = (v, re) => (typeof v === "string" && re.test(v.trim()) ? v.trim().toLowerCase() : null);
  const int = (v) => (v !== "" && v != null && Number.isFinite(Number(v)) && Number(v) >= 0 ? Math.floor(Number(v)) : null);
  // AutoTempest's make/model slugs are lowercase letters and digits only: "mercedesbenz", "cclass", "c63amg", "landrover".
  const slug = (v) => (typeof v === "string" && /^[\w .-]{1,40}$/.test(v.trim()) ? v.toLowerCase().replace(/[^a-z0-9]/g, "") || null : null);
  const q = {
    make: slug(p.make),
    model: slug(p.model),
    zip: text(p.zip, /^\d{5}$/),
    minyear: int(p.minyear), maxyear: int(p.maxyear), minprice: int(p.minprice), maxprice: int(p.maxprice),
    minmiles: int(p.minmiles), maxmiles: int(p.maxmiles),
  };
  if (!q.make || !q.zip) throw new Error("A make and a 5-digit ZIP code are needed.");
  const radius = String(p.radius ?? "50").trim().toLowerCase();
  if (LOCALIZATION.has(radius)) q.localization = radius === "nationwide" ? "country" : radius;
  else if (/^\d{1,4}$/.test(radius)) q.radius = radius;
  else throw new Error("Radius must be miles, state, country or any.");
  return Object.fromEntries(Object.entries(q).filter(([, v]) => v != null && v !== ""));
}

export function searchUrl(params) {
  const q = cleanParams(params);
  const sorted = Object.keys(q).sort().map((k) => [k, q[k]]);
  return "https://www.autotempest.com/results?" + new URLSearchParams(sorted).toString();
}

// ---- functions below run inside the AutoTempest tab (they must not use anything from this module)
function pageRowCount(sel) { return document.querySelectorAll(sel).length; }

function pageClickMore() {
  let clicked = 0;
  for (const b of document.querySelectorAll("button.more-results")) {
    if (b.offsetParent !== null && !b.disabled) { b.click(); clicked++; }
  }
  return clicked;
}

function pageExtract(sel, siteNames) {
  const VIN = /shareListing\(`[^`]*`,\s*`([A-Z0-9]{11,17})`/;
  const seen = new Set();
  const out = [];
  for (const s of document.querySelectorAll(sel)) {
    const a = s.querySelector(".title-wrap.listing-title a.source-link");
    let url = a && a.getAttribute("href");
    if (!url) continue;
    if (url.startsWith("/")) url = "https://www.autotempest.com" + url;
    if (seen.has(url)) continue;
    seen.add(url);
    const priceEl = s.querySelector(".badge__label.label--price");
    const price = priceEl ? ((priceEl.childNodes[0] && priceEl.childNodes[0].textContent) || priceEl.textContent).trim() : null;
    const text = (q) => { const e = s.querySelector(q); return e ? e.textContent.trim() : null; };
    const city = text(".info.location .city"), dealer = text(".dealerName");
    const share = s.querySelector(".share-link button");
    const m = share && VIN.exec(share.getAttribute("onclick") || "");
    const img = s.querySelector(".image");
    const code = s.getAttribute("data-backend-sitecode");
    out.push({
      title: a.textContent.trim(), price, mileage: text(".info.mileageDate .mileage"),
      location: city && dealer ? `${city} — ${dealer}` : city || dealer, listing_url: url,
      image_url: img ? img.getAttribute("data-img") : null, vin: m ? m[1] : null, source_site: siteNames[code] || code,
    });
  }
  return out;
}

const run = async (tabId, func, args = []) => (await chrome.scripting.executeScript({ target: { tabId }, func, args }))[0].result;

export async function runSearch(params, { maxRounds = 15, onProgress = () => {} } = {}) {
  const url = searchUrl(params);
  const tab = await chrome.tabs.create({ url, active: false });
  try {
    onProgress("Waiting for AutoTempest…");
    let n = 0;
    for (let i = 0; i < 60 && n === 0; i++) {            // page load + first cards
      await sleep(500);
      try { n = await run(tab.id, pageRowCount, [ROW]); } catch { /* still navigating */ }
    }
    if (n === 0) throw new Error("AutoTempest showed no results (no matches, or it asked for a human check; open it once in a tab).");

    let last = -1, stable = 0;                           // sources load independently: wait until the count stops growing (~4 s)
    for (let i = 0; i < 40 && stable < 8; i++) {
      await sleep(500);
      n = await run(tab.id, pageRowCount, [ROW]);
      stable = n === last ? stable + 1 : 0;
      last = n;
      if (i % 4 === 0) onProgress(`Loading results… ${n} so far`);
    }
    for (let round = 1; round <= maxRounds; round++) {
      if (!(await run(tab.id, pageClickMore))) break;
      await sleep(1800);
      onProgress(`Loading more results (${round})… ${await run(tab.id, pageRowCount, [ROW])} so far`);
    }
    return await run(tab.id, pageExtract, [ROW, SITE_NAMES]);
  } finally {
    chrome.tabs.remove(tab.id).catch(() => {});
  }
}

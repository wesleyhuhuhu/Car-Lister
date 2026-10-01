// Car Lister Helper: does the work the website can't do from a page (cross-site requests, scraping).
// Requests only come from the page via bridge.js, and only for the fixed actions below.
import { VIN_RE, adapterFor, describeAdapters } from "./adapters/index.js";
import { decodeVins } from "./nhtsa.js";
import { runSearch } from "./search.js";
import { TabClosed } from "./adapters/tabs.js";
import { uploadListings, uploadOptions } from "./upload.js";

const jobs = new Map();           // jobId -> { state, message, result }
let nextId = 1;
const GAP_MS = 1500;              // default pause between lookups on the same site (adapters can ask for more: minGapMs)
const MAX_FAILURES = 3;           // failures in a row on one site before pausing it
const COOLDOWN_MS = 15 * 60_000;  // ...for this long (same as companion.py)
let lookupChain = Promise.resolve();
const siteState = {};             // adapter id -> { last, failures, blockedUntil }

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const parseMoney = (t) => { const d = String(t ?? "").replace(/[^\d]/g, ""); return d ? Number(d) : null; };

function newJob(message) {
  const id = String(nextId++);
  jobs.set(id, { state: "running", message });
  if (jobs.size > 200) jobs.delete(jobs.keys().next().value);
  return id;
}
const finish = (id, patch) => Object.assign(jobs.get(id), patch);

async function searchJob(id, params) {
  const job = jobs.get(id);
  try {
    const found = await runSearch(params, { onProgress: (m) => { job.message = m; } });
    job.message = `Decoding ${found.length} VINs…`;
    const vins = [...new Set(found.map((l) => l.vin).filter((v) => v && VIN_RE.test(v)))];
    const specs = await decodeVins(vins);
    const listings = found.map((l) => {
      const s = specs[l.vin] || {};
      const titleYear = /^(19|20)\d\d/.exec(l.title);
      return {
        ...l, listing_key: l.vin || "url:" + l.listing_url,
        year: s.year ? Number(s.year) : titleYear ? Number(titleYear[0]) : null,
        make: s.make, model: s.model, trim: s.trim,
        price_text: l.price, price: parseMoney(l.price), mileage_text: l.mileage, mileage: parseMoney(l.mileage),
        options: [], local: true,
      };
    });
    let upload = null;
    if (params.share !== false) upload = await uploadListings(listings, (m) => { job.message = m; });
    finish(id, { state: "done", message: `Found ${listings.length} listings.`, result: { listings, upload } });
  } catch (e) {
    finish(id, { state: "failed", message: e.message || String(e) });
  }
}

function fetchJob(id, vin, make, listing, share) {
  const job = jobs.get(id);
  job.state = "queued"; job.message = "Waiting for your turn…";
  lookupChain = lookupChain.then(async () => {
    try {
      const adapter = adapterFor(make, vin);
      if (!adapter) throw new Error("No lookup site is set up for this make yet.");
      if (!adapter.ready) throw new Error(`${adapter.label}: not set up yet.`);
      const site = (siteState[adapter.id] ||= { last: 0, failures: 0, blockedUntil: 0 });
      if (Date.now() < site.blockedUntil) {
        throw new Error(`${adapter.label} isn't answering; paused for ${Math.ceil((site.blockedUntil - Date.now()) / 60_000)} more minutes to stay under its limit.`);
      }
      const wait = (adapter.minGapMs || GAP_MS) - (Date.now() - site.last);
      if (wait > 0) { job.message = `Waiting ${Math.ceil(wait / 1000)} s before the next ${adapter.label} lookup…`; await sleep(wait); }
      job.state = "running"; job.message = `Looking up on ${adapter.label}…`;
      let looked;
      try {
        looked = await adapter.lookup(vin, make, { onMessage: (m) => { job.message = m; } });
        site.failures = 0;
      } catch (e) {
        if (!(e instanceof TabClosed) && ++site.failures >= MAX_FAILURES) { site.failures = 0; site.blockedUntil = Date.now() + COOLDOWN_MS; }
        throw e;
      } finally {
        site.last = Date.now();
      }
      const { sheet, options, codes = [] } = looked;   // codes: only for sites that list "CODE text" options
      const result = { vin, build_sheet: sheet, options, option_codes: codes };
      if (share) { job.message = "Saving to the database…"; result.upload = await uploadOptions(vin, listing, result); }
      finish(id, { state: "done", message: `${options.length} options found.`, result });
    } catch (e) {
      finish(id, { state: "failed", message: e.message || String(e) });
    }
  });
}

const handlers = {
  hello: () => ({ app: "car-lister-extension", version: chrome.runtime.getManifest().version, adapters: describeAdapters() }),

  search: ({ params }) => {
    const id = newJob("Starting search…");
    searchJob(id, params || {});
    return { jobId: id };
  },

  fetchOptions: ({ vin, make, listing, share }) => {
    vin = String(vin || "").trim().toUpperCase();
    if (!VIN_RE.test(vin)) return { error: "That is not a valid 17-character VIN." };
    const id = newJob("Queued…");
    fetchJob(id, vin, make, listing, share !== false);
    return { jobId: id };
  },

  job: ({ jobId }) => {
    const j = jobs.get(String(jobId));
    return j ? { ...j } : { state: "unknown", message: "No such job (the extension may have restarted)." };
  },
};

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || msg.target === "offscreen" || sender.id !== chrome.runtime.id) return;
  const h = handlers[msg.type];
  if (!h) { sendResponse({ error: "unknown request" }); return; }
  Promise.resolve().then(() => h(msg.payload || {})).then(sendResponse, (e) => sendResponse({ error: String(e) }));
  return true;
});

// Car Lister Helper: does the work the website can't do from a page (cross-site requests, scraping).
// Requests only come from the page via bridge.js, and only for the fixed actions below.
import { VIN_RE, adapterFor, describeAdapters } from "./adapters/index.js";
import { decodeVins } from "./nhtsa.js";
import { runSearch } from "./search.js";

const jobs = new Map();           // jobId -> { state, message, result }
let nextId = 1;
const GAP_MS = 1500;              // pause between lookups
let lookupChain = Promise.resolve();
let lastLookup = 0;

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
    finish(id, { state: "done", message: `Found ${listings.length} listings.`, result: listings });
  } catch (e) {
    finish(id, { state: "failed", message: e.message || String(e) });
  }
}

function fetchJob(id, vin, make) {
  const job = jobs.get(id);
  job.state = "queued"; job.message = "Waiting for your turn…";
  lookupChain = lookupChain.then(async () => {
    try {
      const adapter = adapterFor(make, vin);
      if (!adapter) throw new Error("No lookup site is set up for this make yet.");
      if (!adapter.ready) throw new Error(`${adapter.label}: not set up yet.`);
      job.state = "running"; job.message = `Looking up on ${adapter.label}…`;
      const wait = GAP_MS - (Date.now() - lastLookup);
      if (wait > 0) await sleep(wait);
      const { sheet, options, codes = [] } = await adapter.lookup(vin, make);   // codes: only for sites that list "CODE text" options
      finish(id, {
        state: "done", message: `${options.length} options found.`,
        result: { vin, build_sheet: sheet, options, option_codes: codes },
      });
    } catch (e) {
      finish(id, { state: "failed", message: e.message || String(e) });
    } finally {
      lastLookup = Date.now();
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

  fetchOptions: ({ vin, make }) => {
    vin = String(vin || "").trim().toUpperCase();
    if (!VIN_RE.test(vin)) return { error: "That is not a valid 17-character VIN." };
    const id = newJob("Queued…");
    fetchJob(id, vin, make);
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

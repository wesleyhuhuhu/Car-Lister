// BMW / MINI / Rolls-Royce: bimmer.work, run in a real tab of the user's own browser.
// The lookup form is protected by Google reCAPTCHA Enterprise, which the page runs itself when the form is
// submitted; we never create, skip or fake its token. The tab simply does what a person would do:
// open bimmer.work, type the VIN, press Submit, read the vehicle page and its Options page.
// Parsing mirrors scrape_bmw_data.py; if bimmer.work changes its markup, fix both.
import { BMW_TAB_ACTIVE, OEM_FALLBACK } from "../config.js";
import { lookupOem, OemLimitReached } from "./oemnav.js";

const HOME = "https://bimmer.work/";
const BIMMER_RETRY_MS = 10 * 60_000;   // after a block, go straight to the fallback for this long (as bmw_lookup.py)
let bimmerSkipUntil = 0;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// The site refused to show the form or answer (rate limit, block): the only failure that may use a fallback.
export class SiteBlocked extends Error {}

// ---- functions below run inside the bimmer.work tab
function pageFormState() {
  const box = document.querySelector("#vinQuery #vin");
  const button = document.querySelector("#vinQuery button[type=submit], #vinQuery input[type=submit]") ||
    [...document.querySelectorAll("#vinQuery button")].find((b) => /submit/i.test(b.textContent));
  return { ready: !!(box && button), text: document.body ? document.body.innerText.slice(0, 300) : "" };
}

function pageBlocked() {
  const t = document.body ? document.body.innerText : "";
  return /429 Too Many Requests|Too Many Requests|rate limit/i.test(t) ? t.replace(/\s+/g, " ").slice(0, 200) : "";
}

function pageSubmit(vin) {
  const box = document.querySelector("#vinQuery #vin");
  const button = document.querySelector("#vinQuery button[type=submit], #vinQuery input[type=submit]") ||
    [...document.querySelectorAll("#vinQuery button")].find((b) => /submit/i.test(b.textContent));
  box.focus();
  box.value = vin;
  box.dispatchEvent(new Event("input", { bubbles: true }));
  box.dispatchEvent(new Event("change", { bubbles: true }));
  button.click();            // the page's own handler gets the reCAPTCHA token and submits
  return true;
}

function pageVehicle(vin, fields) {
  const ths = [...document.querySelectorAll("th")];
  const cell = (name) => {
    const th = ths.find((t) => t.textContent.replace(/\s+/g, " ").trim() === name);
    const td = th && th.nextElementSibling;
    return td ? td.textContent.replace(/\s+/g, " ").trim() || null : null;
  };
  if (!ths.some((t) => t.textContent.trim() === "Market")) return null;
  const out = {};
  for (const f of fields) out[f] = cell(f);
  const text = document.body.innerText.toUpperCase();
  out.__vinShown = text.includes(vin);
  out.__vinCell = cell("VIN");
  return out;
}

function pageOptions() {
  const out = {};
  for (const tr of document.querySelectorAll("tr")) {
    const b = tr.querySelector("td > b");
    if (!b) continue;
    const segments = [];
    let current = "";
    for (const node of b.parentElement.childNodes) {
      if (node === b) continue;
      if (node.nodeName === "BR") { segments.push(current.trim()); current = ""; } else current += node.textContent;
    }
    segments.push(current.trim());
    const code = b.textContent.trim().toUpperCase();
    if (code) out[code] = segments[1] || segments[2] || "";
  }
  return out;
}

const run = async (tabId, func, args = []) => (await chrome.scripting.executeScript({ target: { tabId }, func, args }))[0].result;

async function waitFor(fn, timeoutMs, stepMs = 500) {
  const end = Date.now() + timeoutMs;
  while (Date.now() < end) {
    try { const v = await fn(); if (v) return v; } catch { /* page navigating */ }
    await sleep(stepMs);
  }
  return null;
}

const VEHICLE_FIELDS = ["Market", "Transmission", "Color", "Upholstery", "Start of Production"];

export const bmw = {
  id: "bmw",
  label: "bimmer.work",
  makes: ["bmw", "mini", "rolls-royce", "rolls royce", "alpina"],
  wmi: ["WBA", "WBS", "WBX", "WBY", "WB1", "WB3", "WB4", "WB5", "WB6", "4US", "5UX", "5UJ", "5YM", "5YA", "WMW", "WMZ", "SCA"],
  ready: true,
  minGapMs: 10_000,          // same pacing as companion.py
  codes: true,

  // Try bimmer.work; if it blocks (429 / no form), use oemnavigations.com in a visible tab (unless OEM_FALLBACK is off).
  async lookup(vin, _make, ctx = {}) {
    const say = ctx.onMessage || (() => {});
    let blocked = null;
    if (!OEM_FALLBACK || Date.now() >= bimmerSkipUntil) {
      try {
        return await this.lookupBimmer(vin);
      } catch (e) {
        if (!(e instanceof SiteBlocked) || !OEM_FALLBACK) throw e;
        blocked = e;
        bimmerSkipUntil = Date.now() + BIMMER_RETRY_MS;
      }
    }
    say("bimmer.work is blocking lookups; trying oemnavigations.com in a new tab (about 2 free checks a day)…");
    try {
      const sheet = await lookupOem(vin, say);
      const codes = Object.keys(sheet.Options);
      return { sheet, options: codes.map((c) => `${c} ${sheet.Options[c]}`.trim()), codes: codes.filter((c) => /^[A-Z0-9]{2,5}$/.test(c)) };
    } catch (e) {
      if (e instanceof OemLimitReached) throw new SiteBlocked(`bimmer.work is blocking lookups and the fallback is used up. ${e.message}${blocked ? " First problem: " + blocked.message : ""}`);
      throw e;
    }
  },

  async lookupBimmer(vin) {
    const tab = await chrome.tabs.create({ url: HOME, active: BMW_TAB_ACTIVE });
    try {
      const form = await waitFor(async () => { const s = await run(tab.id, pageFormState); return s.ready ? s : null; }, 25_000);
      if (!form) {
        let text = "";
        try { text = (await run(tab.id, pageFormState)).text; } catch { /* nothing to read */ }
        throw new SiteBlocked(`bimmer.work did not show its VIN form (rate limit or block?). ${text.replace(/\s+/g, " ").slice(0, 160)}`);
      }
      await run(tab.id, pageSubmit, [vin]);

      const landed = await waitFor(async () => {
        if (/\/vin\/[^/]+/.test((await chrome.tabs.get(tab.id)).url || "")) return "vehicle";
        const blocked = await run(tab.id, pageBlocked);
        return blocked ? "blocked:" + blocked : null;
      }, 45_000);
      if (landed && landed.startsWith("blocked:")) throw new SiteBlocked(`bimmer.work: ${landed.slice(8)}`);
      if (!landed) {
        let text = "";
        try { text = (await run(tab.id, pageFormState)).text; } catch { /* nothing to read */ }
        throw new SiteBlocked(`bimmer.work did not open a vehicle page after Submit. ${text.replace(/\s+/g, " ").slice(0, 160)}`);
      }
      const vehicle = await waitFor(() => run(tab.id, pageVehicle, [vin, VEHICLE_FIELDS]), 40_000);
      if (!vehicle) throw new Error("bimmer.work's vehicle information did not appear in time.");
      const vehicleUrl = (await chrome.tabs.get(tab.id)).url.match(/^https:\/\/[^/]+\/vin\/[^/?#]+/)[0];

      await chrome.tabs.update(tab.id, { url: vehicleUrl + "/options/" });
      const options = await waitFor(async () => {
        const t = await chrome.tabs.get(tab.id);
        if (!/\/options\/?$/.test(new URL(t.url).pathname)) return null;
        const o = await run(tab.id, pageOptions);
        return Object.keys(o).length ? o : null;
      }, 30_000);
      if (!options) throw new Error("No option rows were found on bimmer.work's Options page.");

      const { __vinShown, __vinCell, ...details } = vehicle;
      const sheet = { ...details, Options: options, Details: { ...details }, Source: "bimmer.work" };
      if (__vinShown || (__vinCell && __vinCell.replace(/[^A-Z0-9]/gi, "").toUpperCase() === vin)) sheet.Details.VIN = vin;
      const codes = Object.keys(options);
      return { sheet, options: codes.map((c) => `${c} ${options[c]}`.trim()), codes: codes.filter((c) => /^[A-Z0-9]{2,5}$/.test(c)) };
    } finally {
      chrome.tabs.remove(tab.id).catch(() => {});
    }
  },
};

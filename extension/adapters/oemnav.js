// Fallback for BMW when bimmer.work refuses: https://oemnavigations.com/pages/vin-decoder-app
// About 2 free checks per day; the second one shows a Cloudflare Turnstile human check. That check is never
// bypassed: the lookup runs in a visible, focused tab and the person ticks the box themselves.
// Ported from oem_lookup.py; the build sheet has the same shape as bimmer.work's (codes like "248").
const URL_ = "https://oemnavigations.com/pages/vin-decoder-app";
const RESULT_WAIT_MS = 90_000;
const CAPTCHA_WAIT_MS = 120_000;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const LIMIT_WORDS = /limit|no (more )?(free )?(checks|lookups|searches)|remaining|too many|come back|tomorrow/i;

export class OemLimitReached extends Error {}

// ---- functions below run inside the oemnavigations tab
function pageForm(vin) {
  const box = document.querySelector("#vinNumber");
  const button = document.querySelector("#submit-vin-search");
  if (!box || !button) return "missing";
  const banner = document.querySelector("#maintenance-banner");
  if (banner && banner.offsetParent !== null) return "maintenance";
  if (!vin) return "ready";
  box.value = vin;
  box.dispatchEvent(new Event("input", { bubbles: true }));
  box.dispatchEvent(new Event("change", { bubbles: true }));
  button.click();
  return "submitted";
}

function pageState() {
  const res = document.querySelector("#vin-search-result");
  if (res && res.querySelector(".section-title")) return ["result", ""];
  const cap = document.querySelector("#captcha-container");
  if (cap && getComputedStyle(cap).display !== "none" && cap.children.length) return ["captcha", ""];
  if (res && getComputedStyle(res).display !== "none" && res.innerText.trim()) return ["error", res.innerText.trim().slice(0, 300)];
  return ["waiting", ""];
}

function pageResult() {
  const root = document.querySelector("#vin-search-result");
  const out = { sections: {}, equipment: {} };
  if (!root) return out;
  for (const sec of root.querySelectorAll(".section")) {
    const t = ((sec.querySelector(".section-title") || {}).textContent || "").trim();
    const pairs = {};
    for (const p of sec.querySelectorAll(".data-pair")) {
      const l = ((p.querySelector(".data-label") || {}).textContent || "").trim();
      const v = ((p.querySelector(".data-value") || {}).textContent || "").trim();
      if (l) pairs[l.replace(/:$/, "")] = v;
    }
    const items = [];
    for (const it of sec.querySelectorAll(".equipment-item")) {
      const c = ((it.querySelector(".equipment-code") || {}).textContent || "").trim();
      const text = it.textContent.trim();
      items.push([c, text.startsWith(c) ? text.slice(c.length).trim() : text]);
    }
    if (Object.keys(pairs).length) out.sections[t] = pairs;
    if (items.length) out.equipment[t] = items;
  }
  return out;
}

// ---- pure conversion (same rules as oem_lookup.to_build_sheet)
export const normalizeCode = (code) => {
  const c = String(code || "").trim().toUpperCase();
  return c.length === 4 && c.startsWith("0") ? c.slice(1) : c;
};

export function toBuildSheet(parsed, vin) {
  const equipment = parsed.equipment || {};
  const find = (word) => Object.entries(equipment).find(([k]) => k.toLowerCase().includes(word))?.[1] || [];
  const exWorks = find("ex works");
  const retro = find("retrofit");
  if (!exWorks.length) throw new Error("oemnavigations.com returned a result without an equipment list.");
  const details = {};
  for (const pairs of Object.values(parsed.sections || {})) for (const [k, v] of Object.entries(pairs)) if (v) details[k] = v;
  const shownVin = String(details.VIN || "").replace(/[^A-Za-z0-9]/g, "").toUpperCase();
  if (shownVin && shownVin !== vin) throw new Error("oemnavigations.com answered for a different VIN.");

  const options = {};
  for (const [code, text] of exWorks) if (code) options[normalizeCode(code)] = text;
  const uph = details["Upholstery Code"];
  const uphText = exWorks.find(([c]) => c.toUpperCase() === String(uph || "").toUpperCase())?.[1];
  const sheet = {};
  if (details.Color) sheet.Color = details.Color;
  if (uph) sheet.Upholstery = uphText || uph;
  if (details["Production Date"]) sheet["Start of Production"] = details["Production Date"];
  sheet.Options = options;
  if (retro.length) sheet.Retrofitted = Object.fromEntries(retro.filter(([c]) => c).map(([c, t]) => [normalizeCode(c), t]));
  sheet.Details = { ...details, ...(shownVin ? { VIN: shownVin } : {}) };
  sheet.Source = "oemnavigations.com";
  return sheet;
}

const run = async (tabId, func, args = []) => (await chrome.scripting.executeScript({ target: { tabId }, func, args }))[0].result;

export async function lookupOem(vin, onMessage = () => {}) {
  const tab = await chrome.tabs.create({ url: URL_, active: true });   // visible: the person may need to tick the human check
  try {
    let form = null;
    for (let i = 0; i < 50 && form !== "ready" && form !== "maintenance"; i++) {
      await sleep(400);
      try { form = await run(tab.id, pageForm, [null]); } catch { /* loading */ }
    }
    if (form === "maintenance") throw new Error("oemnavigations.com says its VIN decoder is under maintenance.");
    if (form !== "ready") throw new Error("The oemnavigations.com VIN form did not appear.");
    await run(tab.id, pageForm, [vin]);

    let deadline = Date.now() + RESULT_WAIT_MS, captchaShown = false;
    for (;;) {
      if (Date.now() > deadline) {
        throw new Error(captchaShown
          ? "oemnavigations.com's human check was not completed in time."
          : "oemnavigations.com gave no answer in time.");
      }
      let state = ["waiting", ""];
      try { state = await run(tab.id, pageState); } catch { /* re-rendering */ }
      if (state[0] === "result") break;
      if (state[0] === "error") {
        if (LIMIT_WORDS.test(state[1])) throw new OemLimitReached(`oemnavigations.com: ${state[1]}`);
        throw new Error(`oemnavigations.com: ${state[1]}`);
      }
      if (state[0] === "captcha" && !captchaShown) {
        captchaShown = true;
        deadline = Math.max(deadline, Date.now() + CAPTCHA_WAIT_MS);
        await chrome.tabs.update(tab.id, { active: true });
        const t = await chrome.tabs.get(tab.id);
        await chrome.windows.update(t.windowId, { focused: true });
        onMessage("oemnavigations.com is showing a human check: please tick it in the tab that just opened.");
      }
      await sleep(500);
    }
    return toBuildSheet(await run(tab.id, pageResult), vin);
  } finally {
    chrome.tabs.remove(tab.id).catch(() => {});
  }
}

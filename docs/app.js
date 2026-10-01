import { CONFIG } from "./config.js";
import * as ext from "./extension.js";

const COLUMNS = "listing_key,vin,title,year,make,model,trim,price,price_text,mileage,mileage_text," +
  "source_site,location,listing_url,image_url,options,options_checked_at,updated_at,being_fetched";
const PAGE = 1000;
const $ = (id) => document.getElementById(id);

const state = {
  rows: [],
  jobs: {},                 // vin -> {state, message}
  pinned: new Set(),        // VINs fetched this visit: stay visible under "Without options" until a filter changes
  companion: { ok: false, busy: false, blocked: 0 },
  db: [],                   // rows from the shared database
  local: loadLocal(),       // rows found by this browser's own searches (and options fetched through the extension)
  ext: null,                // {version, adapters} when the helper extension is installed
};

function loadLocal() {
  try { return JSON.parse(localStorage.getItem("carlister.local") || "[]"); } catch { return []; }
}
function saveLocal() {
  try { localStorage.setItem("carlister.local", JSON.stringify(state.local)); } catch { /* storage full or blocked: keep in memory */ }
}
// Database rows win; a local row only fills in what the database does not have yet.
function mergeRows() {
  const byVin = new Map(state.db.filter((r) => r.vin).map((r) => [r.vin, r]));
  const extra = state.local.filter((r) => !(r.vin && byVin.has(r.vin) && hasOptions(byVin.get(r.vin))));
  state.rows = [...state.db.filter((r) => !extra.some((l) => l.vin && l.vin === r.vin)), ...extra];
}

// ---------------------------------------------------------------- helpers
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (n, text) => (n != null ? "$" + Number(n).toLocaleString("en-US") : text || "Price n/a");
const miles = (n, text) => (n != null ? Number(n).toLocaleString("en-US") + " mi" : text || "");
const isBmw = (r) => /^bmw$/i.test(r.make || "") && /^[A-HJ-NPR-Z0-9]{17}$/i.test(r.vin || "");
const hasOptions = (r) => Array.isArray(r.options) && r.options.length > 0;
const adapterOf = (r) => state.ext && state.ext.adapters.find((a) => a.makes.includes(String(r.make || "").toLowerCase()));
const num = (id) => { const v = $(id).value.trim(); return v === "" ? null : Number(v); };
const safeUrl = (u) => (/^https?:\/\//i.test(u || "") ? u : "");

function dbHeaders() {
  const h = { apikey: CONFIG.SUPABASE_ANON_KEY };
  if (CONFIG.SUPABASE_ANON_KEY.startsWith("eyJ")) h.Authorization = "Bearer " + CONFIG.SUPABASE_ANON_KEY;
  return h;
}

// ---------------------------------------------------------------- database
async function dbGet(query, extra = {}) {
  const res = await fetch(`${CONFIG.SUPABASE_URL}/rest/v1/public_listings?${query}`, { headers: { ...dbHeaders(), ...extra } });
  if (!res.ok) throw new Error(`Database error ${res.status}`);
  return res.json();
}

async function rpc(name, args) {
  const res = await fetch(`${CONFIG.SUPABASE_URL}/rest/v1/rpc/${name}`, {
    method: "POST", headers: { ...dbHeaders(), "Content-Type": "application/json" }, body: JSON.stringify(args),
  });
  if (!res.ok) throw new Error(`Database error ${res.status}`);
  return res.json();
}

// A stable name for this browser, used for VIN claims (like companion.py's computer name).
function workerName() {
  try {
    let w = localStorage.getItem("carlister.worker");
    if (!w) { w = "web-" + Math.random().toString(36).slice(2, 10); localStorage.setItem("carlister.worker", w); }
    return w;
  } catch { return "web-anonymous"; }
}

async function loadAll() {
  const rows = [];
  for (let from = 0; ; from += PAGE) {
    const batch = await dbGet(`select=${COLUMNS}&order=updated_at.desc`, { Range: `${from}-${from + PAGE - 1}` });
    rows.push(...batch);
    if (batch.length < PAGE) break;
  }
  return rows;
}

async function refreshRow(vin) {
  try {
    const [fresh] = await dbGet(`select=${COLUMNS}&vin=eq.${encodeURIComponent(vin)}`);
    if (!fresh) return;
    const i = state.db.findIndex((r) => r.vin === vin);
    if (i >= 0) state.db[i] = fresh;
    mergeRows();
    render();
  } catch { /* leave the card as it is */ }
}

// ---------------------------------------------------------------- extension
async function pollExt() {
  const was = !!state.ext;
  state.ext = await ext.hello();
  const pill = $("ext-pill");
  pill.className = "pill " + (state.ext ? "ok" : "off");
  $("ext-text").textContent = state.ext ? "Extension connected" : "Extension not installed";
  if (was !== !!state.ext) render();      // fetch buttons depend on it
}

function openExtDialog() {
  $("ext-download").href = (CONFIG.REPO_URL || "https://github.com/wesleyhuhuhu/Car-Lister").replace(/\/$/, "") + "/releases/latest";
  $("ext-adapters").textContent = state.ext
    ? "Option lookups: " + state.ext.adapters.map((a) => `${a.label} (${a.ready ? "ready" : "not set up yet"})`).join(", ") + "."
    : "";
  $("ext-dialog").showModal();
}

async function runSearch(e) {
  e.preventDefault();
  if (!state.ext) { await pollExt(); if (!state.ext) return openExtDialog(); }
  const go = $("s-go"), status = $("s-status");
  go.disabled = true; status.textContent = "Starting…";
  try {
    const share = $("s-share").checked;
    const { listings: found, upload } = await ext.runJob("search", { params: {
      share,
      make: $("s-make").value.trim(), model: $("s-model").value.trim(), zip: $("s-zip").value.trim(), radius: $("s-radius").value,
      minyear: num("s-minyear"), maxyear: num("s-maxyear"), maxprice: num("s-maxprice"),
    } }, (m) => { status.textContent = m; });
    const now = new Date().toISOString();
    const known = new Map(state.local.map((r) => [r.listing_key, r]));
    for (const l of found) {
      const old = known.get(l.listing_key);
      known.set(l.listing_key, { ...l, updated_at: now, ...(old && hasOptions(old) ? { options: old.options, option_codes: old.option_codes, build_sheet: old.build_sheet } : {}) });
    }
    state.local = [...known.values()];
    saveLocal(); mergeRows(); fillSelects(); render();
    let msg = `Found ${found.length} listings (${state.local.length} saved on this device).`;
    if (upload) {
      if (upload.error) msg += ` Could not add them to the database: ${upload.error}`;
      else {
        msg += ` Database: ${upload.inserted} new, ${upload.updated} refreshed` + (upload.rejected
          ? `, ${upload.rejected} not accepted (${Object.entries(upload.reasons).map(([k, v]) => `${k} x${v}`).join(", ")})` : "") + ".";
        if (upload.inserted || upload.updated) {
          try { state.db = await loadAll(); mergeRows(); fillSelects(); render(); } catch { /* the page keeps what it has */ }
        }
      }
    }
    status.textContent = msg;
  } catch (err) {
    status.textContent = err.message;
  } finally {
    go.disabled = false;
  }
}

async function fetchOptionsExt(r) {
  if (!state.ext) { await pollExt(); if (!state.ext) return openExtDialog(); }
  setJob(r.vin, "running", "Sending to the extension…");
  // Like companion.py: claim a BMW VIN that is in the shared database so two people don't look it up at once.
  let claimed = false;
  if (isBmw(r) && state.db.some((d) => d.vin === r.vin)) {
    try {
      claimed = await rpc("claim_vin", { p_worker: workerName(), p_vin: r.vin });
      if (!claimed) { setJob(r.vin, "failed", "Someone else is fetching this VIN, or it already has options."); return refreshRow(r.vin); }
    } catch { /* database unreachable: look it up anyway; the first saved result wins */ }
  }
  try {
    const listing = { title: r.title, price: r.price, price_text: r.price_text, mileage: r.mileage, mileage_text: r.mileage_text,
      source_site: r.source_site, location: r.location, listing_url: r.listing_url, image_url: r.image_url };
    const res = await ext.runJob("fetchOptions", { vin: r.vin, make: r.make, listing, share: $("s-share").checked },
      (m) => setJob(r.vin, "running", m));
    const i = state.local.findIndex((l) => l.vin === r.vin);
    const patch = { options: res.options, option_codes: res.option_codes, build_sheet: res.build_sheet, options_checked_at: new Date().toISOString() };
    if (i >= 0) state.local[i] = { ...state.local[i], ...patch };
    else state.local.push({ ...r, ...patch, local: true });
    saveLocal(); state.pinned.add(r.vin); mergeRows();
    const up = res.upload;
    const note = !up ? "" : up.stored ? " Added to the database." : up.error ? ` Not added to the database: ${up.error}`
      : up.reason === "already_has_options_or_not_in_database" ? " The database already has options for this car." : ` Not added to the database (${up.reason}).`;
    setJob(r.vin, "done", `${res.options.length} options found.${note}`);
    if (up && (up.stored || up.reason === "already_has_options_or_not_in_database")) refreshRow(r.vin);
    if (claimed && !(up && up.stored)) rpc("release_vins", { p_worker: workerName(), p_vins: [r.vin] }).catch(() => {});
  } catch (err) {
    setJob(r.vin, "failed", err.message);
    if (claimed) rpc("release_vins", { p_worker: workerName(), p_vins: [r.vin] }).catch(() => {});
  }
}

// ---------------------------------------------------------------- companion
async function companion(path, init = {}, timeoutMs = 2500) {
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    return await fetch(CONFIG.COMPANION_URL + path, { ...init, signal: ctl.signal });
  } finally { clearTimeout(t); }
}

async function pollStatus() {
  try {
    const res = await companion("/status");
    const s = await res.json();
    state.companion = { ok: res.ok && s.app !== undefined, busy: !!s.busy, blocked: s.blocked_until_minutes || 0 };
  } catch {
    state.companion = { ok: false, busy: false, blocked: 0 };
  }
  const pill = $("companion-pill"), c = state.companion;
  pill.className = "pill " + (!c.ok ? "off" : c.busy ? "busy" : "ok");
  $("companion-text").textContent = !c.ok ? "Companion not running" : c.blocked > 0 ? "Companion paused" : c.busy ? "Companion working…" : "Companion connected";
}

function setJob(vin, st, message = "") {
  state.jobs[vin] = { state: st, message };
  render();
}

async function fetchOptions(vin) {
  await pollStatus();
  if (!state.companion.ok) return openCompanionDialog();
  setJob(vin, "queued", "Sending to your companion…");
  try {
    const res = await companion("/fetch", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ vin }) }, 10000);
    const body = await res.json().catch(() => ({}));
    if (res.status === 202) return trackJob(vin);
    if (res.status === 409) { setJob(vin, "failed", body.message || "Someone else already has this VIN."); return refreshRow(vin); }
    if (res.status === 429) return setJob(vin, "failed", `bimmer.work isn't answering. Try again in ${Math.ceil(body.retry_after_minutes || 15)} min.`);
    setJob(vin, "failed", body.error || body.message || `Companion error ${res.status}`);
  } catch {
    setJob(vin, "failed", "Could not reach the companion. Is it still running?");
    pollStatus();
  }
}

async function trackJob(vin) {
  for (;;) {
    try {
      const res = await companion(`/job?vin=${encodeURIComponent(vin)}`);
      const job = await res.json();
      if (job.state === "done") {
        state.pinned.add(vin);
        setJob(vin, "done", job.message || "Options fetched.");
        return refreshRow(vin);
      }
      if (job.state === "failed" || job.state === "unknown") return setJob(vin, "failed", job.message || "Lookup failed.");
      setJob(vin, job.state, job.message || (job.state === "queued" ? "Waiting for your turn…" : "Looking it up (about 20–40 s)…"));
    } catch {
      return setJob(vin, "failed", "Lost contact with the companion.");
    }
    await new Promise((r) => setTimeout(r, 2500));
  }
}

function openCompanionDialog() {
  const repo = (CONFIG.REPO_URL || "https://github.com/wesleyhuhuhu/Car-Lister").replace(/\/$/, "");
  $("companion-clone").textContent = `git clone ${repo}.git\ncd ${repo.split("/").pop()}`;
  $("companion-cmd").textContent = `python companion.py --allow-origin ${location.origin}`;
  $("companion-dialog").showModal();
}

// ---------------------------------------------------------------- filtering
function terms(id) { return $(id).value.toLowerCase().split(/\s+/).filter(Boolean); }

function filtered() {
  const q = terms("f-search"), o = terms("f-options");
  const make = $("f-make").value, model = $("f-model").value, trim = $("f-trim").value;
  const yMin = num("f-year-min"), yMax = num("f-year-max"), pMax = num("f-price-max"), mMax = num("f-miles-max");
  const has = $("f-has").value;
  const out = state.rows.filter((r) => {
    if (make && r.make !== make) return false;
    if (model && r.model !== model) return false;
    if (trim && r.trim !== trim) return false;
    if (yMin != null && !(r.year >= yMin)) return false;
    if (yMax != null && !(r.year <= yMax)) return false;
    if (pMax != null && !(r.price != null && r.price <= pMax)) return false;
    if (mMax != null && !(r.mileage != null && r.mileage <= mMax)) return false;
    if (has === "with" && !hasOptions(r)) return false;
    if (has === "without" && hasOptions(r) && !state.pinned.has(r.vin)) return false;
    if (q.length) {
      const hay = `${r.title || ""} ${r.vin || ""} ${r.location || ""}`.toLowerCase();
      if (!q.every((t) => hay.includes(t))) return false;
    }
    if (o.length) {
      const hay = (r.options || []).join(" | ").toLowerCase();
      if (!o.every((t) => hay.includes(t))) return false;
    }
    return true;
  });
  const by = {
    "new": (a, b) => String(b.updated_at).localeCompare(String(a.updated_at)),
    "price-asc": (a, b) => (a.price ?? Infinity) - (b.price ?? Infinity),
    "price-desc": (a, b) => (b.price ?? -1) - (a.price ?? -1),
    "miles-asc": (a, b) => (a.mileage ?? Infinity) - (b.mileage ?? Infinity),
    "year-desc": (a, b) => (b.year ?? 0) - (a.year ?? 0),
  }[$("f-sort").value];
  return out.sort(by);
}

function fillSelect(id, values) {
  const sel = $(id), keep = sel.value;
  const first = sel.options[0].outerHTML;
  sel.innerHTML = first + [...new Set(values.filter(Boolean))].sort().map((v) => `<option>${esc(v)}</option>`).join("");
  sel.value = [...sel.options].some((o) => o.value === keep) ? keep : "";
}

function fillSelects() {
  fillSelect("f-make", state.rows.map((r) => r.make));
  fillSelect("f-model", state.rows.filter((r) => !$("f-make").value || r.make === $("f-make").value).map((r) => r.model));
  fillSelect("f-trim", state.rows.filter((r) => (!$("f-make").value || r.make === $("f-make").value) &&
    (!$("f-model").value || r.model === $("f-model").value)).map((r) => r.trim));
}

// ---------------------------------------------------------------- rendering
function highlight(text, ts) {
  let html = esc(text);
  for (const t of ts) html = html.replace(new RegExp(t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "gi"), (m) => `<mark>${m}</mark>`);
  return html;
}

function optionsBlock(r, ts) {
  const items = r.options.map((o) => {
    if (!r.option_codes || !r.option_codes.length) return `<li>${highlight(o, ts)}</li>`;   // window stickers have names, not codes
    const [code, ...rest] = o.split(" ");
    return `<li><b>${highlight(code, ts)}</b>${highlight(rest.join(" "), ts)}</li>`;
  }).join("");
  return `<details class="opts" ${ts.length || state.pinned.has(r.vin) ? "open" : ""}><summary>${r.options.length} factory options</summary><ul>${items}</ul></details>`;
}

function actionArea(r) {
  const job = state.jobs[r.vin];
  const link = safeUrl(r.listing_url)
    ? `<a class="btn" href="${esc(safeUrl(r.listing_url))}" target="_blank" rel="noopener noreferrer">View listing${r.source_site ? " on " + esc(r.source_site) : ""}</a>` : "";
  let fetchBit = "";
  const working = job && (job.state === "queued" || job.state === "running");
  const validVin = /^[A-HJ-NPR-Z0-9]{17}$/i.test(r.vin || "");
  if (!hasOptions(r) && isBmw(r) && state.ext && adapterOf(r) && adapterOf(r).ready) {
    if (working) fetchBit = `<button class="btn primary" disabled>Fetching…</button>`;
    else if (r.being_fetched) fetchBit = `<button class="btn" disabled>Being fetched by someone…</button>`;
    else fetchBit = `<button class="btn primary" data-fetch-ext="${esc(r.vin)}">Fetch options</button>`;
  } else if (!hasOptions(r) && isBmw(r)) {
    if (working) fetchBit = `<button class="btn primary" disabled>Fetching…</button>`;
    else if (r.being_fetched) fetchBit = `<button class="btn" disabled>Being fetched by someone…</button>`;
    else fetchBit = `<button class="btn primary" data-fetch="${esc(r.vin)}">Fetch options</button>`;
  } else if (!hasOptions(r) && validVin && adapterOf(r)) {
    if (working) fetchBit = `<button class="btn primary" disabled>Fetching…</button>`;
    else if (!adapterOf(r).ready) fetchBit = `<button class="btn" disabled title="No lookup site is set up for this make yet">Lookup not set up</button>`;
    else fetchBit = `<button class="btn primary" data-fetch-ext="${esc(r.vin)}">Fetch options</button>`;
  }
  let status = "";
  if (job && job.message) {
    const cls = job.state === "done" ? "ok" : job.state === "failed" ? "bad" : "";
    status = `<span class="status ${cls}" role="status">${esc(job.message)}</span>`;
  }
  return `<div class="actions">${fetchBit}${link}${status}</div>`;
}

function card(r, ts) {
  const img = safeUrl(r.image_url)
    ? `<img class="thumb" loading="lazy" alt="" src="${esc(safeUrl(r.image_url))}" referrerpolicy="no-referrer" onerror="this.style.visibility='hidden'">`
    : `<div class="thumb"></div>`;
  const meta = [r.year, r.mileage != null || r.mileage_text ? miles(r.mileage, r.mileage_text) : "", r.location].filter(Boolean)
    .map((m) => `<span>${esc(m)}</span>`).join("");
  return `<article class="card">${img}<div class="body">
    <h2 class="title">${esc(r.title || [r.year, r.make, r.model, r.trim].filter(Boolean).join(" "))}</h2>
    <div class="price">${esc(money(r.price, r.price_text))}</div>
    <div class="meta">${meta}</div>
    ${r.vin ? `<div class="vin">${esc(r.vin)}</div>` : ""}
    ${hasOptions(r) ? optionsBlock(r, ts) : `<span class="badge">Options not fetched yet</span>`}
    ${actionArea(r)}
  </div></article>`;
}

function render() {
  const rows = filtered(), ts = terms("f-options");
  $("count").textContent = `${rows.length.toLocaleString()} of ${state.rows.length.toLocaleString()} listings`;
  $("grid").innerHTML = rows.map((r) => card(r, ts)).join("");
  $("empty").hidden = rows.length > 0 || state.rows.length === 0;
}

// ---------------------------------------------------------------- start
function wire() {
  const changed = () => { state.pinned.clear(); render(); };   // a filter edit ends the "keep it visible" grace
  for (const id of ["f-search", "f-options", "f-year-min", "f-year-max", "f-price-max", "f-miles-max"]) $(id).addEventListener("input", changed);
  for (const id of ["f-has", "f-sort"]) $(id).addEventListener("change", changed);
  for (const id of ["f-make", "f-model", "f-trim"]) $(id).addEventListener("change", () => { fillSelects(); changed(); });
  $("f-reset").addEventListener("click", () => {
    state.pinned.clear();
    for (const el of document.querySelectorAll(".filters input, .filters select")) el.value = "";
    $("f-sort").value = "new"; fillSelects(); render();
  });
  $("grid").addEventListener("click", (e) => {
    const b = e.target.closest("[data-fetch]");
    if (b) return fetchOptions(b.dataset.fetch);
    const x = e.target.closest("[data-fetch-ext]");
    if (x) { const r = state.rows.find((row) => row.vin === x.dataset.fetchExt); if (r) fetchOptionsExt(r); }
  });
  $("search-form").addEventListener("submit", runSearch);
  $("ext-pill").addEventListener("click", openExtDialog);
  $("companion-pill").addEventListener("click", openCompanionDialog);
}

async function main() {
  document.title = CONFIG.SITE_TITLE; $("site-title").textContent = CONFIG.SITE_TITLE;
  wire();
  pollStatus(); setInterval(pollStatus, 8000);
  pollExt(); setInterval(pollExt, 15000);
  if (CONFIG.SUPABASE_URL.includes("YOUR-PROJECT")) {
    $("error").hidden = false; $("error").textContent = "Set SUPABASE_URL and SUPABASE_ANON_KEY in config.js.";
    return;
  }
  $("count").textContent = "Loading listings…";
  try {
    state.db = await loadAll();
  } catch (e) {
    $("error").hidden = false;
    $("error").textContent = "Could not load shared listings (" + e.message + "). Showing what is saved on this device.";
  }
  mergeRows(); fillSelects(); render();
}
main();

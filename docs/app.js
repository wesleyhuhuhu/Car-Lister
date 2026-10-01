import { CONFIG } from "./config.js";
import * as ext from "./extension.js";

const BASE_COLUMNS = "listing_key,vin,title,year,make,model,trim,price,price_text,mileage,mileage_text," +
  "source_site,location,listing_url,image_url,options,option_codes,options_checked_at,updated_at,being_fetched";
// color / interior come from schema_shared.sql's public_listings view; older databases don't have them yet.
let COLUMNS = BASE_COLUMNS + ",color,interior";
const PAGE = 1000;          // rows per database request
const SHOW_STEP = 60;       // cards rendered per "Show more"
const LATEST = 9;           // listings fetched on first load; the rest only when someone filters, sorts or asks for more
const $ = (id) => document.getElementById(id);

const state = {
  rows: [],
  jobs: {},                 // vin -> {state, message}
  pinned: new Set(),        // VINs fetched this visit: stay visible under "Not fetched yet" until a filter changes
  companion: { ok: false, busy: false, blocked: 0 },
  ext: null,                // {version, adapters} when the helper extension is installed
  near: null,               // { origin: [lat, lon], radius: miles | null } from the "Near ZIP" filter
  shown: SHOW_STEP,
  makesLoaded: false,
  loaded: false,            // the first request has answered (until then the page says "Loading listings…")
  full: false,              // false while only the LATEST newest listings are loaded
  total: null,              // how many listings the database holds (from the first request's Content-Range)
  fullLoading: null,
};

// The shared database is the only source of listings: searches and fetched options are saved there and the
// page re-reads it, so every visitor sees the same rows. Earlier versions kept copies in localStorage; clear them.
try { localStorage.removeItem("carlister.local"); localStorage.removeItem("carlister.worker"); } catch { /* storage blocked */ }

// Name used for VIN claims during this visit (like companion.py's computer name). Claims expire after 30 minutes.
const WORKER = "web-" + Math.random().toString(36).slice(2, 10);

// ---------------------------------------------------------------- helpers
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (n, text) => (n != null ? "$" + Number(n).toLocaleString("en-US") : text || "Price n/a");
const miles = (n, text) => (n != null ? Number(n).toLocaleString("en-US") + " mi" : text || "");
const isBmw = (r) => /^bmw$/i.test(r.make || "") && /^[A-HJ-NPR-Z0-9]{17}$/i.test(r.vin || "");
const hasOptions = (r) => Array.isArray(r.options) && r.options.length > 0;
const adapterOf = (r) => state.ext && state.ext.adapters.find((a) => a.makes.includes(String(r.make || "").toLowerCase()));
const num = (id) => { const v = $(id).value.trim(); return v === "" ? null : Number(v); };
const safeUrl = (u) => (/^https?:\/\//i.test(u || "") ? u : "");
const same = (a, b) => String(a || "").toLowerCase().replace(/[^a-z0-9]/g, "") === String(b || "").toLowerCase().replace(/[^a-z0-9]/g, "");

// NHTSA spells makes in capitals ("MERCEDES-BENZ"); show them the way people write them.
const KEEP_CAPS = new Set(["BMW", "GMC", "MINI", "RAM", "AMC", "SRT"]);
function prettyMake(m) {
  if (!m || m !== m.toUpperCase() || KEEP_CAPS.has(m)) return m || "";
  return m.toLowerCase().replace(/(^|[\s-])[a-z]/g, (c) => c.toUpperCase());
}

function dbHeaders() {
  const h = { apikey: CONFIG.SUPABASE_ANON_KEY };
  if (CONFIG.SUPABASE_ANON_KEY.startsWith("eyJ")) h.Authorization = "Bearer " + CONFIG.SUPABASE_ANON_KEY;
  return h;
}

// ---------------------------------------------------------------- database
async function dbGet(query, extra = {}) {
  const res = await fetch(`${CONFIG.SUPABASE_URL}/rest/v1/public_listings?${query}`, { headers: { ...dbHeaders(), ...extra } });
  if (!res.ok) {
    const err = new Error(`Database error ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

async function rpc(name, args) {
  const res = await fetch(`${CONFIG.SUPABASE_URL}/rest/v1/rpc/${name}`, {
    method: "POST", headers: { ...dbHeaders(), "Content-Type": "application/json" }, body: JSON.stringify(args),
  });
  if (!res.ok) throw new Error(`Database error ${res.status}`);
  return res.json();
}

async function loadAll() {
  const rows = [];
  for (let from = 0; ; from += PAGE) {
    let batch;
    try {
      batch = await dbGet(`select=${COLUMNS}&order=updated_at.desc`, { Range: `${from}-${from + PAGE - 1}` });
    } catch (e) {
      if (e.status === 400 && COLUMNS !== BASE_COLUMNS) { COLUMNS = BASE_COLUMNS; return loadAll(); }   // view not updated yet
      throw e;
    }
    rows.push(...batch);
    if (batch.length < PAGE) break;
  }
  return rows;
}

// First load: only the newest few, plus the total count, so the page shows listings straight away.
async function loadLatest() {
  const res = await fetch(`${CONFIG.SUPABASE_URL}/rest/v1/public_listings?select=${COLUMNS}&order=updated_at.desc&limit=${LATEST}`,
    { headers: { ...dbHeaders(), Prefer: "count=exact" } });
  if (res.status === 400 && COLUMNS !== BASE_COLUMNS) { COLUMNS = BASE_COLUMNS; return loadLatest(); }   // view not updated yet
  if (!res.ok) throw new Error(`Database error ${res.status}`);
  const total = Number((res.headers.get("Content-Range") || "").split("/")[1]);
  return { rows: await res.json(), total: Number.isFinite(total) ? total : null };
}

// Everything, the first time someone needs it (filters, sort and "Show all" work over every listing).
function ensureFull() {
  if (state.full) return Promise.resolve();
  if (!state.fullLoading) {
    $("count").textContent = "Loading all listings…";
    state.fullLoading = loadAll().then((rows) => {
      state.rows = rows; state.full = true; state.total = rows.length; state.loaded = true;
      fillSelects(); render();
    }).catch((e) => {
      state.fullLoading = null;
      $("error").hidden = false; $("error").textContent = "Could not load all listings. " + e.message;
      throw e;
    });
  }
  return state.fullLoading;
}
const afterFull = (fn) => (...args) => ensureFull().then(() => fn(...args), () => {});

async function refreshRow(vin) {
  try {
    const [fresh] = await dbGet(`select=${COLUMNS}&vin=eq.${encodeURIComponent(vin)}`);
    if (!fresh) return;
    const i = state.rows.findIndex((r) => r.vin === vin);
    if (i >= 0) state.rows[i] = fresh; else state.rows.push(fresh);
    render();
  } catch { /* leave the card as it is */ }
}

// ---------------------------------------------------------------- distance ("Near ZIP")
// docs/geo.json is built by tools/build_geo.py from the US Census: ZIP -> [lat, lon] and "ST|cityname" -> [lat, lon].
let geo = null, geoLoading = null;
function loadGeo() {
  geoLoading = geoLoading || fetch("geo.json").then((r) => { if (!r.ok) throw new Error("geo.json " + r.status); return r.json(); })
    .then((g) => { geo = g; return g; }).catch((e) => { geoLoading = null; throw e; });
  return geoLoading;
}
// Must normalise exactly like key() in tools/build_geo.py.
function geoKey(st, city) {
  let c = city.toLowerCase().trim().replace(/\btownship\b|\btwp\.?/g, "");
  c = c.replace(/saint /g, "st ").replace(/sainte /g, "ste ").replace(/mount /g, "mt ").replace(/fort /g, "ft ");
  return st.toUpperCase() + "|" + c.replace(/[^a-z0-9]/g, "");
}
function placeOf(r) {
  if (r._geo !== undefined) return r._geo;
  const m = /^(.*?),\s*([A-Z]{2})\b/.exec(String(r.location || "").split(" — ")[0].trim());
  r._geo = m && geo ? geo.places[geoKey(m[2], m[1])] || null : null;
  return r._geo;
}
function distanceMiles([la1, lo1], [la2, lo2]) {
  const rad = Math.PI / 180, dLa = (la2 - la1) * rad, dLo = (lo2 - lo1) * rad;
  const a = Math.sin(dLa / 2) ** 2 + Math.cos(la1 * rad) * Math.cos(la2 * rad) * Math.sin(dLo / 2) ** 2;
  return 3958.8 * 2 * Math.asin(Math.sqrt(a));
}

async function updateNear() {
  const zip = $("f-zip").value.trim(), radius = num("f-radius"), note = $("f-zip-note");
  note.hidden = true; note.className = "hint";
  if (!/^\d{5}$/.test(zip)) {
    state.near = null;
    if (zip) { note.hidden = false; note.textContent = "Enter a 5-digit ZIP."; }
  } else {
    try {
      await loadGeo();
      const origin = geo.zips[zip];
      if (!origin) { state.near = null; note.hidden = false; note.className = "hint is-bad"; note.textContent = `ZIP ${zip} wasn't found.`; }
      else state.near = { origin, radius };
    } catch {
      state.near = null; note.hidden = false; note.className = "hint is-bad"; note.textContent = "Distances couldn't be loaded.";
    }
  }
  const distOpt = $("f-sort").querySelector('option[value="distance"]');
  distOpt.disabled = !state.near;
  if (!state.near && $("f-sort").value === "distance") $("f-sort").value = "new";
}

// ---------------------------------------------------------------- extension
async function pollExt() {
  const was = !!state.ext;
  state.ext = await ext.hello();
  const pill = $("ext-pill");
  pill.className = "pill " + (state.ext ? "ok" : "off");
  $("ext-text").textContent = state.ext ? "Extension connected" : "Extension not installed";
  if (was !== !!state.ext) { render(); loadMakes(); }      // fetch buttons and the search form depend on it
}

function openExtDialog() {
  $("ext-download").href = (CONFIG.REPO_URL || "https://github.com/wesleyhuhuhu/Car-Lister").replace(/\/$/, "") + "/releases/latest";
  $("ext-adapters").textContent = state.ext
    ? "Option lookups: " + state.ext.adapters.map((a) => `${a.label} (${a.ready ? "ready" : "not set up yet"})`).join(", ") + "."
    : "";
  $("ext-dialog").showModal();
}

// ---------------------------------------------------------------- search form (AutoTempest's own make / model lists)
const option = (value, text, depth = 0) => `<option value="${esc(value)}">${"   ".repeat(depth)}${esc(text)}</option>`;

async function loadMakes() {
  const sel = $("s-make");
  if (!state.ext) {
    sel.innerHTML = `<option value="">Needs the extension</option>`;
    sel.disabled = true; state.makesLoaded = false;
    return;
  }
  if (state.makesLoaded) return;
  sel.disabled = true; sel.innerHTML = `<option value="">Loading makes…</option>`;
  try {
    const { popular, all } = await ext.call("makes", {}, 20000);
    sel.innerHTML = `<option value="">Select a make</option>` +
      `<optgroup label="Popular makes">${popular.map(([v, t]) => option(v, t)).join("")}</optgroup>` +
      `<optgroup label="All makes">${all.map(([v, t]) => option(v, t)).join("")}</optgroup>`;
    sel.disabled = false; state.makesLoaded = true;
  } catch (e) {
    sel.innerHTML = `<option value="">Couldn't load makes, reload to retry</option>`;
  }
}

async function loadModels() {
  const make = $("s-make").value, sel = $("s-model");
  sel.disabled = true;
  if (!make) { sel.innerHTML = `<option value="">Pick a make</option>`; return; }
  sel.innerHTML = `<option value="">Loading models…</option>`;
  try {
    const { popular, all } = await ext.call("models", { make }, 20000);
    if ($("s-make").value !== make) return;                       // changed again meanwhile
    const opts = (list) => list.map(([v, t, level]) => option(v, t, Math.min(level, 2))).join("");
    sel.innerHTML = `<option value="">Any model</option>` +
      (popular.length ? `<optgroup label="Popular models">${opts(popular)}</optgroup>` : "") +
      `<optgroup label="All models">${opts(all)}</optgroup>`;
  } catch {
    sel.innerHTML = `<option value="">Any model (list unavailable)</option>`;
  }
  sel.disabled = false;
}

function setSearchStatus(text, kind = "") {
  const el = $("s-status");
  el.textContent = text;
  el.className = "search-status" + (kind ? " is-" + kind : "");
}

async function runSearch(e) {
  e.preventDefault();
  if (!state.ext) { await pollExt(); if (!state.ext) return openExtDialog(); }
  const go = $("s-go");
  const params = {
    share: true,
    make: $("s-make").value, model: $("s-model").value, trim: $("s-trim").value.trim(),
    zip: $("s-zip").value.trim(), radius: $("s-radius").value,
    minyear: num("s-minyear"), maxyear: num("s-maxyear"), maxprice: num("s-maxprice"),
  };
  go.disabled = true; setSearchStatus("Starting…");
  try {
    const { listings: found, upload } = await ext.runJob("search", { params }, (m) => setSearchStatus(m));
    if (!upload || upload.error) {
      setSearchStatus(`Found ${found.length} listings, but they could not be saved to the database, so they are not shown: ` +
        `${(upload && upload.error) || "no answer"}. Try the search again.`, "bad");
      return;
    }
    let msg = `Found ${found.length} listings: ${upload.inserted} new, ${upload.updated} refreshed` + (upload.rejected
      ? `, ${upload.rejected} not accepted (${Object.entries(upload.reasons).map(([k, v]) => `${k.replace(/_/g, " ")} ×${v}`).join(", ")})` : "") + ".";
    try {
      state.rows = await loadAll(); state.full = true; state.total = state.rows.length; state.loaded = true;
      showSearchInFilters(params);
    } catch (err) { msg += ` Reload the page to see them (${err.message}).`; }
    setSearchStatus(msg, "ok");
  } catch (err) {
    setSearchStatus(err.message, "bad");
  } finally {
    go.disabled = false;
  }
}

// After a search, point the filters at what was searched so the new listings are what you see.
async function showSearchInFilters(p) {
  for (const el of document.querySelectorAll(".filters input, .filters select")) el.value = "";
  const makeName = $("s-make").selectedOptions[0]?.textContent.trim();
  const modelName = $("s-model").value ? $("s-model").selectedOptions[0]?.textContent.trim() : "";
  fillSelects();
  const dbMake = [...$("f-make").options].find((o) => o.value && same(o.value, makeName));
  if (dbMake) {
    $("f-make").value = dbMake.value; fillSelects();
    const dbModel = modelName && [...$("f-model").options].find((o) => o.value && same(o.value, modelName));
    if (dbModel) { $("f-model").value = dbModel.value; fillSelects(); }
  }
  if (/^\d{5}$/.test(p.zip)) {
    $("f-zip").value = p.zip;
    if ([...$("f-radius").options].some((o) => o.value === p.radius)) $("f-radius").value = p.radius;
  }
  if (p.minyear) $("f-year-min").value = p.minyear;
  if (p.maxyear) $("f-year-max").value = p.maxyear;
  if (p.maxprice) $("f-price-max").value = p.maxprice;
  await updateNear();
  if (state.near) $("f-sort").value = "distance";
  filtersChanged();
}

// ---------------------------------------------------------------- fetch options (extension)
async function fetchOptionsExt(r) {
  if (!state.ext) { await pollExt(); if (!state.ext) return openExtDialog(); }
  setJob(r.vin, "running", "Sending to the extension…");
  // Like companion.py: claim a BMW VIN that is in the shared database so two people don't look it up at once.
  let claimed = false;
  if (isBmw(r) && state.rows.some((d) => d.vin === r.vin)) {
    try {
      claimed = await rpc("claim_vin", { p_worker: WORKER, p_vin: r.vin });
      if (!claimed) { setJob(r.vin, "failed", "Someone else is fetching this VIN, or it already has options."); return refreshRow(r.vin); }
    } catch { /* database unreachable: look it up anyway; the first saved result wins */ }
  }
  try {
    const listing = { title: r.title, price: r.price, price_text: r.price_text, mileage: r.mileage, mileage_text: r.mileage_text,
      source_site: r.source_site, location: r.location, listing_url: r.listing_url, image_url: r.image_url };
    const res = await ext.runJob("fetchOptions", { vin: r.vin, make: r.make, listing, share: true },
      (m) => setJob(r.vin, "running", m));
    const up = res.upload || { error: "no answer from the database" };
    if (up.stored) {
      state.pinned.add(r.vin);
      setJob(r.vin, "done", `${res.options.length} options found and saved.`);
    } else if (up.reason === "already_has_options_or_not_in_database") {
      setJob(r.vin, "done", "The database already has options for this car.");
    } else {
      setJob(r.vin, "failed", `${res.options.length} options found, but the database did not accept them: ${up.error || up.reason}.`);
    }
    refreshRow(r.vin);
    if (claimed && !(up && up.stored)) rpc("release_vins", { p_worker: WORKER, p_vins: [r.vin] }).catch(() => {});
  } catch (err) {
    setJob(r.vin, "failed", err.message);
    if (claimed) rpc("release_vins", { p_worker: WORKER, p_vins: [r.vin] }).catch(() => {});
  }
}

// ---------------------------------------------------------------- companion (BMW without the extension)
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
  pill.className = "pill pill--quiet " + (!c.ok ? "off" : c.busy ? "busy" : "ok");
  $("companion-text").textContent = !c.ok ? "Companion off" : c.blocked > 0 ? "Companion paused" : c.busy ? "Companion working…" : "Companion on";
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
  const has = $("f-has").value, near = state.near;
  let unknownPlace = 0;
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
    if (near) {
      const p = placeOf(r);
      r._dist = p ? distanceMiles(near.origin, p) : null;
      if (near.radius != null) {
        if (r._dist == null) { unknownPlace++; return false; }
        if (r._dist > near.radius) return false;
      }
    }
    return true;
  });
  const by = {
    "new": (a, b) => String(b.updated_at).localeCompare(String(a.updated_at)),
    "price-asc": (a, b) => (a.price ?? Infinity) - (b.price ?? Infinity),
    "price-desc": (a, b) => (b.price ?? -1) - (a.price ?? -1),
    "miles-asc": (a, b) => (a.mileage ?? Infinity) - (b.mileage ?? Infinity),
    "year-desc": (a, b) => (b.year ?? 0) - (a.year ?? 0),
    "distance": (a, b) => (a._dist ?? Infinity) - (b._dist ?? Infinity),
  }[$("f-sort").value] || (() => 0);
  return { rows: out.sort(by), unknownPlace };
}

// Model is only offered once a make is chosen, and trim once a model is chosen, each listing only what exists for it.
function fillSelect(id, values, { label, placeholder, enabled, pretty = (v) => v }) {
  const sel = $(id), keep = sel.value;
  if (!enabled) { sel.innerHTML = `<option value="">${esc(placeholder)}</option>`; sel.disabled = true; return; }
  const counts = new Map();
  for (const v of values) if (v) counts.set(v, (counts.get(v) || 0) + 1);
  sel.innerHTML = `<option value="">${esc(label)}</option>` + [...counts.keys()].sort((a, b) => pretty(a).localeCompare(pretty(b)))
    .map((v) => `<option value="${esc(v)}">${esc(pretty(v))} (${counts.get(v)})</option>`).join("");
  sel.disabled = false;
  sel.value = counts.has(keep) ? keep : "";
}

function fillSelects() {
  const make = $("f-make").value;
  fillSelect("f-make", state.rows.map((r) => r.make), { label: "All makes", enabled: true, pretty: prettyMake });
  const makeNow = $("f-make").value;
  if (makeNow !== make) $("f-model").value = "";
  fillSelect("f-model", state.rows.filter((r) => r.make === makeNow).map((r) => r.model),
    { label: "All models", placeholder: "Select a make first", enabled: !!makeNow });
  const modelNow = $("f-model").value;
  fillSelect("f-trim", state.rows.filter((r) => r.make === makeNow && r.model === modelNow).map((r) => r.trim),
    { label: "All trims", placeholder: "Select a model first", enabled: !!(makeNow && modelNow) });
}

function activeFilterCount() {
  const ids = ["f-search", "f-options", "f-make", "f-model", "f-trim", "f-year-min", "f-year-max", "f-price-max", "f-miles-max", "f-has"];
  return ids.filter((id) => $(id).value.trim() !== "").length + (state.near && state.near.radius != null ? 1 : 0);
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

// A best guess at the paint from its name ("Black Sapphire Metallic", "Brooklyn Grau", "Bright White Clear-Coat"...).
const SWATCHES = [
  [/black|schwarz|noir|nero|carbon|obsidian|onyx|jet|ebony|sapphire/i, "#16181d"],
  [/white|weiss|weiß|blanc|bianco|alpine|pearl|ivory|snow|frozen white|mineral white/i, "#f4f4f2"],
  [/silver|silber|argent|platinum|aluminum|aluminium|titanium/i, "#c3c7cc"],
  [/gr[ae]y|grau|gris|grigio|graphite|gunmetal|slate|charcoal|ash|cement|granite|anthracite/i, "#7d838c"],
  [/blue|blau|bleu|blu|navy|marina|portimao|tanzanite|azure|cobalt|ocean|sky/i, "#2459a8"],
  [/red|rot|rouge|rosso|crimson|ruby|garnet|scarlet|cherry|toronto|melbourne|aventurin/i, "#b22424"],
  [/green|gr[uü]n|vert|verde|emerald|olive|isle of man|british racing|sage|forest/i, "#2f6b3a"],
  [/yellow|gelb|jaune|giallo|austin|sao paulo|são paulo|lemon|sunflower/i, "#e7c11d"],
  [/orange|fire|sunset|valencia|copper|kupfer|tangerine/i, "#dd6b20"],
  [/brown|braun|brun|marrone|mocha|espresso|bronze|chestnut|cognac|tartufo|havana/i, "#6b4630"],
  [/beige|tan|sand|cream|champagne|fawn|taupe|oyster|mojave/i, "#d2c2a0"],
  [/gold|oro|dore/i, "#c9a227"],
  [/purple|violet|lila|plum|amethyst|twilight/i, "#5b3d8f"],
];
function swatch(name) {
  let best = null, at = Infinity;
  for (const [re, color] of SWATCHES) {
    const m = re.exec(name || "");
    if (m && m.index < at) { at = m.index; best = color; }
  }
  return best ? `<span class="swatch" style="background:${best}"></span>` : `<span class="swatch swatch--unknown"></span>`;
}

function colorsBlock(r) {
  const rows = [];
  if (r.color) rows.push(`<div>${swatch(r.color)}<b>Exterior</b><span class="val" title="${esc(r.color)}">${esc(r.color)}</span></div>`);
  if (r.interior) rows.push(`<div>${swatch(r.interior)}<b>Interior</b><span class="val" title="${esc(r.interior)}">${esc(r.interior)}</span></div>`);
  return rows.length ? `<div class="colors">${rows.join("")}</div>` : "";
}

function actionArea(r) {
  const job = state.jobs[r.vin];
  const link = safeUrl(r.listing_url)
    ? `<a class="btn" href="${esc(safeUrl(r.listing_url))}" target="_blank" rel="noopener noreferrer">View listing</a>` : "";
  let fetchBit = "";
  const working = job && (job.state === "queued" || job.state === "running");
  const validVin = /^[A-HJ-NPR-Z0-9]{17}$/i.test(r.vin || "");
  if (!hasOptions(r) && isBmw(r) && state.ext && adapterOf(r) && adapterOf(r).ready) {
    if (working) fetchBit = `<button class="btn btn--primary" disabled>Fetching…</button>`;
    else if (r.being_fetched) fetchBit = `<button class="btn" disabled>Being fetched…</button>`;
    else fetchBit = `<button class="btn btn--primary" data-fetch-ext="${esc(r.vin)}">Fetch options</button>`;
  } else if (!hasOptions(r) && isBmw(r)) {
    if (working) fetchBit = `<button class="btn btn--primary" disabled>Fetching…</button>`;
    else if (r.being_fetched) fetchBit = `<button class="btn" disabled>Being fetched…</button>`;
    else fetchBit = `<button class="btn btn--primary" data-fetch="${esc(r.vin)}">Fetch options</button>`;
  } else if (!hasOptions(r) && validVin && adapterOf(r)) {
    if (working) fetchBit = `<button class="btn btn--primary" disabled>Fetching…</button>`;
    else if (!adapterOf(r).ready) fetchBit = `<button class="btn" disabled title="No lookup site is set up for this make yet">Lookup not set up</button>`;
    else fetchBit = `<button class="btn btn--primary" data-fetch-ext="${esc(r.vin)}">Fetch options</button>`;
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
    ? `<img loading="lazy" alt="" src="${esc(safeUrl(r.image_url))}" referrerpolicy="no-referrer" onerror="this.remove()">` : "";
  const dist = r._dist != null && state.near ? `<span class="chip-dist">${Math.round(r._dist).toLocaleString("en-US")} mi away</span>` : "";
  const source = r.source_site ? `<span class="chip-source">${esc(r.source_site)}</span>` : "";
  const place = String(r.location || "").split(" — ")[0];
  const meta = [
    r.mileage != null || r.mileage_text ? miles(r.mileage, r.mileage_text) : "",
    place,
    r.trim && !String(r.title || "").toLowerCase().includes(String(r.trim).toLowerCase()) ? r.trim : "",
  ].filter(Boolean).map((m) => `<span title="${esc(m)}">${esc(m)}</span>`).join("");
  return `<article class="card">
    <div class="media">${img}${source}${dist}</div>
    <div class="body">
      <h2 class="title">${esc(r.title || [r.year, prettyMake(r.make), r.model, r.trim].filter(Boolean).join(" "))}</h2>
      <div class="price">${esc(money(r.price, r.price_text))}</div>
      <div class="meta">${meta}</div>
      ${colorsBlock(r)}
      ${r.vin ? `<div class="vin">${esc(r.vin)}</div>` : ""}
      ${hasOptions(r) ? optionsBlock(r, ts) : `<span class="badge">Options not fetched yet</span>`}
      ${actionArea(r)}
    </div></article>`;
}

function render() {
  if (!state.loaded) return;                // nothing to show yet: keep "Loading listings…"
  const { rows, unknownPlace } = filtered(), ts = terms("f-options");
  const shown = rows.slice(0, state.shown);
  if (!state.full) {
    const total = state.total ?? state.rows.length;
    $("count").innerHTML = `The <strong>${shown.length}</strong> newest of ${total.toLocaleString()} listings`;
    $("grid").innerHTML = shown.map((r) => card(r, ts)).join("");
    $("more-wrap").hidden = total <= shown.length;
    $("more").textContent = `Show all ${total.toLocaleString()} listings`;
  } else {
    $("count").innerHTML = `<strong>${rows.length.toLocaleString()}</strong> of ${state.rows.length.toLocaleString()} listings` +
      (unknownPlace ? ` <span title="Their city couldn't be placed on the map">(${unknownPlace} with an unknown location hidden)</span>` : "");
    $("grid").innerHTML = shown.map((r) => card(r, ts)).join("");
    $("more-wrap").hidden = rows.length <= shown.length;
    $("more").textContent = `Show more (${(rows.length - shown.length).toLocaleString()} left)`;
  }
  $("empty").hidden = rows.length > 0 || state.rows.length === 0;
  const n = activeFilterCount();
  $("filter-count").hidden = !n; $("filter-count").textContent = n;
}

function filtersChanged() {
  state.pinned.clear();          // a filter edit ends the "keep it visible" grace
  state.shown = SHOW_STEP;
  render();
}

// ---------------------------------------------------------------- start
function wire() {
  // Filters and sort work over every listing: touching them loads the rest (once) before applying.
  for (const el of [$("filters-panel"), $("f-sort")]) el.addEventListener("focusin", () => ensureFull().catch(() => {}));
  for (const id of ["f-search", "f-options", "f-year-min", "f-year-max", "f-price-max", "f-miles-max"]) $(id).addEventListener("input", afterFull(filtersChanged));
  for (const id of ["f-has", "f-sort"]) $(id).addEventListener("change", afterFull(filtersChanged));
  for (const id of ["f-make", "f-model", "f-trim"]) $(id).addEventListener("change", afterFull(() => { fillSelects(); filtersChanged(); }));
  const nearChanged = afterFull(async () => { await updateNear(); filtersChanged(); });
  $("f-zip").addEventListener("input", () => { if (/^\d{5}$/.test($("f-zip").value.trim()) || !$("f-zip").value.trim()) nearChanged(); });
  $("f-radius").addEventListener("change", nearChanged);
  $("f-reset").addEventListener("click", afterFull(async () => {
    for (const el of document.querySelectorAll(".filters input, .filters select")) el.value = "";
    $("f-sort").value = "new";
    fillSelects(); await updateNear(); filtersChanged();
  }));
  $("more").addEventListener("click", () => {
    if (!state.full) return ensureFull().catch(() => {});     // "Show all": the first full page
    state.shown += SHOW_STEP; render();
  });
  $("grid").addEventListener("click", (e) => {
    const b = e.target.closest("[data-fetch]");
    if (b) return fetchOptions(b.dataset.fetch);
    const x = e.target.closest("[data-fetch-ext]");
    if (x) { const r = state.rows.find((row) => row.vin === x.dataset.fetchExt); if (r) fetchOptionsExt(r); }
  });
  $("s-make").addEventListener("change", loadModels);
  $("search-form").addEventListener("submit", runSearch);
  $("ext-pill").addEventListener("click", openExtDialog);
  $("companion-pill").addEventListener("click", openCompanionDialog);
  if (matchMedia("(max-width: 960px)").matches) $("filters-panel").open = false;    // filters start folded on phones
}

async function main() {
  document.title = CONFIG.SITE_TITLE; $("site-title").textContent = CONFIG.SITE_TITLE;
  wire();
  pollStatus(); setInterval(pollStatus, 8000);
  pollExt().then(() => { if (!state.ext) loadMakes(); }); setInterval(pollExt, 15000);
  if (CONFIG.SUPABASE_URL.includes("YOUR-PROJECT")) {
    $("error").hidden = false; $("error").textContent = "Set SUPABASE_URL and SUPABASE_ANON_KEY in config.js.";
    return;
  }
  $("count").textContent = "Loading listings…";
  try {
    const { rows, total } = await loadLatest();
    state.rows = rows; state.total = total; state.loaded = true;
    if (total != null && total <= rows.length) { state.full = true; }    // a tiny database: that was everything
    fillSelects(); render();
  } catch (e) {
    $("count").textContent = ""; $("error").hidden = false;
    $("error").textContent = "Could not load listings. " + e.message;
  }
}
main();

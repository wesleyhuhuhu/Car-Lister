import { CONFIG } from "./config.js";

const COLUMNS = "listing_key,vin,title,year,make,model,trim,price,price_text,mileage,mileage_text," +
  "source_site,location,listing_url,image_url,options,options_checked_at,updated_at,being_fetched";
const PAGE = 1000;
const $ = (id) => document.getElementById(id);

const state = {
  rows: [],
  jobs: {},                 // vin -> {state, message}
  companion: { ok: false, busy: false, blocked: 0 },
};

// ---------------------------------------------------------------- helpers
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (n, text) => (n != null ? "$" + Number(n).toLocaleString("en-US") : text || "Price n/a");
const miles = (n, text) => (n != null ? Number(n).toLocaleString("en-US") + " mi" : text || "");
const isBmw = (r) => /^bmw$/i.test(r.make || "") && /^[A-HJ-NPR-Z0-9]{17}$/i.test(r.vin || "");
const hasOptions = (r) => Array.isArray(r.options) && r.options.length > 0;
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
    const i = state.rows.findIndex((r) => r.vin === vin);
    if (i >= 0) state.rows[i] = fresh;
    render();
  } catch { /* leave the card as it is */ }
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
    if (has === "without" && hasOptions(r)) return false;
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
    const [code, ...rest] = o.split(" ");
    return `<li><b>${highlight(code, ts)}</b>${highlight(rest.join(" "), ts)}</li>`;
  }).join("");
  return `<details class="opts" ${ts.length ? "open" : ""}><summary>${r.options.length} factory options</summary><ul>${items}</ul></details>`;
}

function actionArea(r) {
  const job = state.jobs[r.vin];
  const link = safeUrl(r.listing_url)
    ? `<a class="btn" href="${esc(safeUrl(r.listing_url))}" target="_blank" rel="noopener noreferrer">View listing${r.source_site ? " on " + esc(r.source_site) : ""}</a>` : "";
  let fetchBit = "";
  if (!hasOptions(r) && isBmw(r)) {
    const working = job && (job.state === "queued" || job.state === "running");
    if (working) fetchBit = `<button class="btn primary" disabled>Fetching…</button>`;
    else if (r.being_fetched) fetchBit = `<button class="btn" disabled>Being fetched by someone…</button>`;
    else fetchBit = `<button class="btn primary" data-fetch="${esc(r.vin)}">Fetch options</button>`;
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
  for (const id of ["f-search", "f-options", "f-year-min", "f-year-max", "f-price-max", "f-miles-max"]) $(id).addEventListener("input", render);
  for (const id of ["f-has", "f-sort"]) $(id).addEventListener("change", render);
  for (const id of ["f-make", "f-model", "f-trim"]) $(id).addEventListener("change", () => { fillSelects(); render(); });
  $("f-reset").addEventListener("click", () => {
    for (const el of document.querySelectorAll(".filters input, .filters select")) el.value = "";
    $("f-sort").value = "new"; fillSelects(); render();
  });
  $("grid").addEventListener("click", (e) => {
    const b = e.target.closest("[data-fetch]");
    if (b) fetchOptions(b.dataset.fetch);
  });
  $("companion-pill").addEventListener("click", openCompanionDialog);
}

async function main() {
  document.title = CONFIG.SITE_TITLE; $("site-title").textContent = CONFIG.SITE_TITLE;
  wire();
  pollStatus(); setInterval(pollStatus, 8000);
  if (CONFIG.SUPABASE_URL.includes("YOUR-PROJECT")) {
    $("error").hidden = false; $("error").textContent = "Set SUPABASE_URL and SUPABASE_ANON_KEY in config.js.";
    return;
  }
  $("count").textContent = "Loading listings…";
  try {
    state.rows = await loadAll();
    fillSelects(); render();
  } catch (e) {
    $("count").textContent = ""; $("error").hidden = false;
    $("error").textContent = "Could not load listings. " + e.message;
  }
}
main();

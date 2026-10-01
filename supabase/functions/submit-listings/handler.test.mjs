import assert from "assert";
import { vinCheckDigitOk, hostAllowed, cleanRow, specsFromVpic, withSpecs, handle } from "./handler.mjs";

// check digit
assert.ok(vinCheckDigitOk("1M8GDM9AXKP042788"));          // the textbook example
for (const v of ["1C6SRFHMXNN112704", "4T1K61AK9MU614925", "1C4PJXDGXTW187003"]) assert.ok(vinCheckDigitOk(v), v);
assert.ok(!vinCheckDigitOk("1C6SRFHMXNN112705"));
assert.ok(!vinCheckDigitOk("1C6SRFHM5NN112704"));

// hosts
assert.ok(hostAllowed("https://www.cargurus.com/Cars/link/123?x=1"));
assert.ok(hostAllowed("https://cars.com/vehicledetail/abc"));
assert.ok(!hostAllowed("https://evil-cars.com/x"));
assert.ok(!hostAllowed("https://cars.com.evil.example/x"));
assert.ok(!hostAllowed("javascript:alert(1)"));
assert.ok(hostAllowed("https://dealer.example/x", ["dealer.example"]));

// cleanRow
const good = { vin: "1c6srfhmxnn112704", title: "2022 RAM 1500 Limited", listing_url: "https://www.cars.com/x", image_url: "https://thumb.autotempest.com/a.jpg", price: 64000, price_text: "$64,000", mileage: "12000" };
const c = cleanRow(good);
assert.equal(c.vin, "1C6SRFHMXNN112704"); assert.equal(c.mileage, 12000); assert.equal(c.image_url, good.image_url);
assert.equal(cleanRow({ ...good, image_url: "https://evil.example/a.jpg" }).image_url, null);
assert.equal(cleanRow({ ...good, vin: "123" }).reject, "bad_vin");
assert.equal(cleanRow({ ...good, vin: "1C6SRFHMXNN112705" }).reject, "bad_check_digit");
assert.match(cleanRow({ ...good, listing_url: "https://phish.example/login" }).reject, /^listing_url_not_allowed:phish.example/);
assert.equal(cleanRow({ ...good, title: "  " }).reject, "no_title");
assert.equal(cleanRow({ ...good, price: -5 }).price, null);

// specs + title check
assert.equal(specsFromVpic({ ModelYear: "", Make: "" }), null);
const specs = specsFromVpic({ ModelYear: "2022", Make: "RAM", Model: "1500", Trim: "Limited", ErrorCode: "0" });
assert.deepEqual(specs, { year: 2022, make: "RAM", model: "1500", trim: "Limited" });
assert.equal(withSpecs(c, specs).make, "RAM");
assert.equal(withSpecs({ ...c, title: "2015 RAM 1500" }, specs).reject, "title_year_mismatch");
assert.equal(withSpecs({ ...c, title: "2023 RAM 1500" }, specs).reject, undefined);   // model year vs listing year may differ by 1

// whole handler with fake network
function deps({ vpicOk = true, calls = [] } = {}) {
  return {
    supabaseUrl: "https://x.supabase.co", serviceKey: "svc", calls,
    fetch: async (url, init) => {
      calls.push({ url, init });
      if (url.includes("vpic")) {
        if (!vpicOk) return new Response("down", { status: 500 });
        const vins = new URLSearchParams(init.body).get("data").split(";");
        const known = { "1C6SRFHMXNN112704": { ModelYear: "2022", Make: "RAM", Model: "1500", Trim: "Limited" } };
        return Response.json({ Results: vins.map((v) => ({ VIN: v, ...(known[v] || { ModelYear: "", Make: "" }) })) });
      }
      return Response.json([{ inserted: JSON.parse(init.body).p_rows.length, updated: 0, skipped: 0 }]);
    },
  };
}
const post = (body) => new Request("https://f/submit-listings", { method: "POST", body: JSON.stringify(body) });

let calls = [];
let res = await handle(post({ listings: [good, { ...good, vin: "1C6SRFHMXNN112705" }, { ...good, vin: "4T1K61AK9MU614925" }] }), deps({ calls }));
let out = await res.json();
assert.equal(res.status, 200);
assert.equal(out.inserted, 1);
assert.deepEqual(out.rejected.map((r) => r.reason).sort(), ["bad_check_digit", "vin_not_recognized"]);
const sent = JSON.parse(calls.find((c) => c.url.includes("rpc")).init.body).p_rows;
assert.equal(sent.length, 1); assert.equal(sent[0].make, "RAM"); assert.equal(sent[0].year, 2022);

calls = [];
res = await handle(post({ listings: [{ ...good, make: "Ferrari", year: 1999 }] }), deps({ calls }));
assert.equal(JSON.parse(calls.find((c) => c.url.includes("rpc")).init.body).p_rows[0].make, "RAM");   // caller's make is ignored

calls = [];
res = await handle(post({ listings: [good] }), deps({ vpicOk: false, calls }));
assert.equal(res.status, 503);
assert.ok(!calls.some((c) => c.url.includes("rpc")), "nothing may be written when vPIC is down");

assert.equal((await handle(post({ listings: [] }), deps())).status, 400);
assert.equal((await handle(post({ listings: new Array(501).fill(good) }), deps())).status, 400);
assert.equal((await handle(new Request("https://f", { method: "OPTIONS" }), deps())).status, 204);
assert.equal((await handle(new Request("https://f"), deps())).status, 405);

// ---- options action
function optDeps({ patchRows = [{ vin: "1C6SRFHMXNN112704" }], calls = [] } = {}) {
  const d = deps({ calls });
  const base = d.fetch;
  d.fetch = async (url, init) => {
    if (url.includes("/rest/v1/listings?")) { calls.push({ url, init }); return Response.json(patchRows); }
    return base(url, init);
  };
  return d;
}
const sheet = { Details: { VIN: "1C6SRFHMXNN112704", "Model Year": "2022" }, Options: { "Trailer-Tow Group": "$995" }, Source: "www.ramtrucks.com window sticker" };
const opt = { vin: "1c6srfhmxnn112704", build_sheet: sheet, options: ["Trailer-Tow Group $995", "Heated Seats (standard)"], listing: good };

calls = [];
res = await handle(post({ options: opt }), optDeps({ calls }));
out = await res.json();
assert.deepEqual(out, { stored: true });
const patch = calls.find((c) => c.url.includes("/rest/v1/listings?"));
assert.equal(patch.init.method, "PATCH");
assert.match(patch.url, /vin=eq\.1C6SRFHMXNN112704&build_sheet=is\.null/);     // first writer wins: never overwrites options
assert.deepEqual(JSON.parse(patch.init.body).option_codes, []);
assert.ok(calls.some((c) => c.url.includes("rpc/submit_listings")), "the listing is added first so the row exists");

const reason = async (o, d = optDeps()) => (await (await handle(post({ options: o }), d)).json()).reason;
assert.equal(await reason({ ...opt, build_sheet: { ...sheet, Details: { ...sheet.Details, VIN: "1C6SRFHMXNN112706" } } }), "sheet_vin_mismatch");
assert.equal(await reason({ ...opt, build_sheet: { Options: {} } }), "sheet_vin_mismatch");
assert.equal(await reason({ ...opt, build_sheet: { ...sheet, Details: { ...sheet.Details, "Model Year": "2015" } } }), "sheet_year_mismatch");
assert.equal(await reason({ ...opt, options: [] }), "bad_options");
assert.equal(await reason({ ...opt, options: [42] }), "bad_options");
assert.equal(await reason({ ...opt, option_codes: ["not a code"] }), "bad_option_codes");
assert.equal(await reason({ ...opt, build_sheet: undefined }), "build_sheet_missing");
assert.equal(await reason(opt, optDeps({ patchRows: [] })), "already_has_options_or_not_in_database");
assert.equal(await reason({ ...opt, vin: "4T1K61AK9MU614925", build_sheet: { ...sheet, Details: { VIN: "4T1K61AK9MU614925" } } }), "vin_not_recognized");
assert.equal((await handle(post({ options: { ...opt, vin: "1C6SRFHMXNN112705" } }), optDeps())).status, 400);
calls = [];
assert.equal((await handle(post({ options: opt }), Object.assign(optDeps({ calls }), { fetch: async (u) => (u.includes("vpic") ? new Response("x", { status: 500 }) : Response.json([])) }))).status, 503);
// a rejected listing (bad link) does not stop the options from being stored for a row that already exists
calls = [];
res = await handle(post({ options: { ...opt, listing: { ...good, listing_url: "https://phish.example/x" } } }), optDeps({ calls }));
assert.deepEqual(await res.json(), { stored: true });
assert.ok(!calls.some((c) => c.url.includes("rpc/submit_listings")));
console.log("ok: submit-listings");

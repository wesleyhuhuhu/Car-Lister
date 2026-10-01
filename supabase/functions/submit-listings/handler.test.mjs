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
console.log("ok: submit-listings");

// Year/make/model/trim from NHTSA's free vPIC batch decoder (up to 50 VINs per request).
const URL_ = "https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVINValuesBatch/";
const EMPTY = new Set(["", "null", "not applicable"]);
const val = (rec, key) => {
  const v = String(rec[key] ?? "").trim();
  return EMPTY.has(v.toLowerCase()) ? null : v;
};

export async function decodeVins(vins) {
  const out = {};
  for (let i = 0; i < vins.length; i += 50) {
    const batch = vins.slice(i, i + 50);
    try {
      const res = await fetch(URL_, {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({ format: "json", data: batch.join(";") }),
      });
      if (!res.ok) continue;
      for (const rec of (await res.json()).Results || []) {
        const vin = String(rec.VIN || "").toUpperCase();
        if (vin) out[vin] = { year: val(rec, "ModelYear"), make: val(rec, "Make"), model: val(rec, "Model"), trim: val(rec, "Trim") };
      }
    } catch { /* leave these undecoded; the listing still shows */ }
  }
  return out;
}

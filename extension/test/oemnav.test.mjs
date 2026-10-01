import assert from "assert";
import { toBuildSheet, normalizeCode } from "../adapters/oemnav.js";

assert.equal(normalizeCode("0248"), "248");
assert.equal(normalizeCode("01CB"), "1CB");
assert.equal(normalizeCode("S2TB"), "S2TB");
const parsed = {
  sections: { Vehicle: { VIN: "WBS43AY0XRFS41778", Color: "Black Sapphire Metallic (475)", "Upholstery Code": "X3SW", "Production Date": "2024-01-10" } },
  equipment: { "Ex works equipment": [["0248", "Steering wheel heater"], ["X3SW", "Full leather Merino/black"], ["01CR", "Remote engine start"]],
               "Retrofitted equipment": [["06AK", "Connected Drive"]] },
};
const s = toBuildSheet(parsed, "WBS43AY0XRFS41778");
assert.deepEqual(Object.keys(s.Options), ["248", "X3SW", "1CR"]);
assert.equal(s.Upholstery, "Full leather Merino/black");
assert.equal(s["Start of Production"], "2024-01-10");
assert.equal(s.Details.VIN, "WBS43AY0XRFS41778");
assert.deepEqual(s.Retrofitted, { "6AK": "Connected Drive" });
assert.throws(() => toBuildSheet(parsed, "WBS43AY05PFP77387"), /different VIN/);
assert.throws(() => toBuildSheet({ sections: {}, equipment: {} }, "WBS43AY0XRFS41778"), /without an equipment list/);
console.log("ok: oemnav");

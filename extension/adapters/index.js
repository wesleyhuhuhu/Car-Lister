// One adapter per manufacturer group. The page sends the make from the listing; the WMI (first 3
// characters of the VIN) is the fallback when the make is missing or spelled differently.
import { stellantis } from "./stellantis.js";
import { mercedes } from "./mercedes.js";
import { toyota } from "./toyota.js";

export const ADAPTERS = [stellantis, mercedes, toyota];
export const VIN_RE = /^[A-HJ-NPR-Z0-9]{17}$/;

export function adapterFor(make, vin) {
  const m = String(make || "").trim().toLowerCase();
  return ADAPTERS.find((a) => a.makes.includes(m)) || ADAPTERS.find((a) => a.wmi.includes(String(vin).slice(0, 3))) || null;
}

export function describeAdapters() {
  return ADAPTERS.map((a) => ({ id: a.id, label: a.label, makes: a.makes, ready: a.ready }));
}

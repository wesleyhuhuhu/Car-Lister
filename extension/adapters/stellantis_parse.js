// Turns the text lines of a Stellantis (Chrysler / Dodge / Jeep / Ram / Fiat / Alfa Romeo)
// window sticker into a build sheet. Pure function, no browser APIs, so it is unit tested in Node.
//
// The sticker looks like:
//   2022 MODEL YEAR / RAM 1500 LIMITED CREW CAB 4X4 / Base Price: $64,095 / Exterior Color: ...
//   STANDARD EQUIPMENT ... (group headings in CAPS, then one feature per line)
//   OPTIONAL EQUIPMENT ... ("Name  $price" starts a group; unpriced lines after it are what it includes;
//                           "Customer Preferred Package 28M" carries the package code)
//   Destination Charge / TOTAL PRICE

const dash = (s) => s.replace(/[‐-―]/g, "-").replace(/\s+/g, " ").trim();
const PRICE_END = /\s*(\$[\d,]+(?:\.\d\d)?)\s*\*?$/;
const FIELD = /^(Base Price|Exterior Color|Interior Color|Interior|Engine|Transmission|Axle Ratio|Fuel Economy):\s*(.*)$/i;
const PACKAGE = /^(.*?\bPackage)\s+([0-9A-Z]{2,4})$/;

export function parseStellantisSticker(rawLines) {
  const lines = rawLines.map(dash).filter(Boolean);
  const sheet = { Details: {}, Options: {}, Standard: [], Optional: [] };
  let section = "head";
  let group = "";
  let current = null;           // the priced optional group that unpriced lines attach to
  let pkg = null;

  for (const line of lines) {
    const up = line.toUpperCase();
    if (up.startsWith("STANDARD EQUIPMENT")) { section = "standard"; continue; }
    if (up.startsWith("OPTIONAL EQUIPMENT")) { section = "optional"; continue; }
    if (up.startsWith("WARRANTY COVERAGE")) { section = "tail"; }

    if (section === "head") {
      const y = line.match(/^((?:19|20)\d\d) MODEL YEAR$/i);
      if (y) { sheet.Details["Model Year"] = y[1]; continue; }
      const f = line.match(FIELD);
      if (f) {
        sheet.Details[f[1].replace(/\w/g, (c) => c.toUpperCase())] = f[2].trim();
        continue;
      }
      if (!sheet.Details.Model && /^[A-Z0-9][A-Z0-9 /&.\-]+$/.test(line) && !/PRICE|MANUFACTURED|VEHICLE/.test(up)) {
        sheet.Details.Model = line;
      }
    } else if (section === "standard") {
      if (/^[A-Z0-9 /&.\-]+$/.test(line) && /[A-Z]{4}/.test(line) && !/^\d/.test(line)) { group = line; continue; }
      sheet.Standard.push(line);
    } else if (section === "optional") {
      if (/^Destination Charge/i.test(line)) {
        sheet.Details["Destination Charge"] = (line.match(PRICE_END) || [])[1] || "";
        section = "tail"; continue;
      }
      const pk = line.match(PACKAGE);
      if (pk && !PRICE_END.test(line)) {          // "Customer Preferred Package 28M"
        pkg = { code: pk[2], name: pk[1] };
        sheet.Options[pk[2]] = pk[1];
        continue;
      }
      const priced = line.match(PRICE_END);
      if (priced) {
        const name = line.replace(PRICE_END, "").trim();
        current = { name, price: priced[1], includes: [], package: pkg ? pkg.code : undefined };
        sheet.Optional.push(current);
      } else if (current) {
        current.includes.push(line);
      } else {
        sheet.Optional.push({ name: line, price: "", includes: [] });
      }
    } else if (section === "tail") {
      const total = line.match(/^TOTAL PRICE:?\s*\*?\s*(\$[\d,]+)/i);
      if (total) sheet.Details["Total Price"] = total[1];
      const asm = line.match(/^Assembly Point\/Port of Entry:\s*(.*?)(?:\s+S\.L\.)?$/i);
      if (asm) sheet.Details["Assembly Plant"] = asm[1].trim();
    }
  }

  // Map the optional list onto the shared "Options" object (code or name -> text).
  for (const o of sheet.Optional) {
    const text = o.price ? `${o.name} (${o.price})` : o.name;
    sheet.Options[o.name] = o.price ? o.price : "";
    for (const inc of o.includes) sheet.Options[inc] = `in ${o.name}`;
    o.text = text;
  }
  return sheet;
}

// Flat strings for the listings page ("options" column): optional equipment first, then standard.
export function optionStrings(sheet) {
  const out = [];
  for (const o of sheet.Optional) {
    out.push(o.price ? `${o.name} ${o.price}` : o.name);
    for (const inc of o.includes) out.push(`${inc} (in ${o.name})`);
  }
  for (const s of sheet.Standard) out.push(`${s} (standard)`);
  return out;
}

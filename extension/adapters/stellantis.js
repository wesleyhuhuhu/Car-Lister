// Stellantis (Chrysler, Dodge, Jeep, Ram, Fiat, Alfa Romeo): the brand sites serve the original
// window sticker PDF for a VIN from one shared backend, no login or captcha:
//   https://www.<brand>.com/hostd/windowsticker/getWindowStickerPdf.do?vin=<VIN>
// An unknown VIN gets a ~1 KB PDF saying "unable to retrieve a window sticker", real stickers are 50 KB+.
import { parseStellantisSticker, optionStrings } from "./stellantis_parse.js";
import { pdfToLines } from "../pdf.js";

const HOSTS = {
  ram: "www.ramtrucks.com", jeep: "www.jeep.com", dodge: "www.dodge.com", chrysler: "www.chrysler.com",
  fiat: "www.fiatusa.com", "alfa romeo": "www.alfaromeousa.com",
};
const MIN_REAL_PDF_BYTES = 5000;

export const stellantis = {
  id: "stellantis",
  label: "Stellantis window sticker",
  makes: ["chrysler", "dodge", "jeep", "ram", "fiat", "alfa romeo"],
  wmi: ["1C3", "1C4", "1C6", "1C8", "2C3", "2C4", "2C8", "3C4", "3C6", "3C7", "1D4", "1D7", "1D8", "1B3", "1B4", "1B7",
        "3D4", "3D7", "2B3", "2D4", "ZAR", "3C3", "ZFB", "3C8"],
  ready: true,

  async lookup(vin, make) {
    // Every brand site serves the same backend, so if one answers with an error (e.g. a bot filter), try the others.
    const first = HOSTS[String(make || "").toLowerCase()] || HOSTS.chrysler;
    const hosts = [first, ...Object.values(HOSTS).filter((h) => h !== first)];
    let res, host, lastError;
    for (host of hosts) {
      try {
        res = await fetch(`https://${host}/hostd/windowsticker/getWindowStickerPdf.do?vin=${vin}`, { credentials: "omit" });
        if (res.ok) break;
        lastError = `${host} answered HTTP ${res.status}.`;
      } catch (e) { lastError = `${host}: ${e.message}`; }
      res = null;
    }
    if (!res) throw new Error(lastError || "Could not reach the Stellantis sites.");
    const buf = await res.arrayBuffer();
    if (!/pdf/i.test(res.headers.get("content-type") || "") || buf.byteLength < MIN_REAL_PDF_BYTES) {
      throw new Error("Stellantis has no window sticker for this VIN (very old, not yet sold new, or not a Stellantis VIN).");
    }
    const sheet = parseStellantisSticker(await pdfToLines(buf));
    if (!sheet.Optional.length && !sheet.Standard.length) throw new Error("The window sticker could not be read.");
    if (sheet.Details.VIN !== vin) throw new Error("The window sticker does not show this VIN, so it was not used.");
    sheet.Source = `${host} window sticker`;
    return { sheet, options: optionStrings(sheet) };
  },
};

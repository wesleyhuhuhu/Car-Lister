// Reads PDF text through the offscreen document (see offscreen.js).
let creating = null;

async function ensureOffscreen() {
  if (await chrome.offscreen.hasDocument()) return;
  creating = creating || chrome.offscreen
    .createDocument({ url: "offscreen.html", reasons: ["DOM_PARSER"], justification: "Read text out of window sticker PDFs" })
    .finally(() => { creating = null; });
  await creating;
}

function toBase64(buf) {
  const bytes = new Uint8Array(buf);
  let bin = "";
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(bin);
}

export async function pdfToLines(buf) {
  await ensureOffscreen();
  const res = await chrome.runtime.sendMessage({ target: "offscreen", type: "pdf-lines", base64: toBase64(buf) });
  if (!res || res.error) throw new Error("Could not read the PDF: " + ((res && res.error) || "no answer"));
  return res.lines;
}

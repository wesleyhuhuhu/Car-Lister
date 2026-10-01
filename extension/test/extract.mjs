// Dev helper: node test/extract.mjs file.pdf  -> prints the lines the extension's offscreen page would produce
import fs from "fs";
import pdfjs from "pdfjs-dist/legacy/build/pdf.js";
export async function pdfLines(buf) {
  const doc = await pdfjs.getDocument({ data: new Uint8Array(buf), verbosity: 0 }).promise;
  const lines = [];
  for (let p = 1; p <= doc.numPages; p++) {
    let line = "";
    for (const it of (await (await doc.getPage(p)).getTextContent()).items) {
      line += it.str;
      if (it.hasEOL) { lines.push(line); line = ""; }
    }
    if (line) lines.push(line);
  }
  return lines;
}

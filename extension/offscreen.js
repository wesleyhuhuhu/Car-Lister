// Offscreen document: the service worker has no DOM/Workers for pdf.js, so PDF text is read here.
pdfjsLib.GlobalWorkerOptions.workerSrc = chrome.runtime.getURL("vendor/pdf.worker.min.js");

async function pdfLines(base64) {
  const bytes = Uint8Array.from(atob(base64), (c) => c.charCodeAt(0));
  const doc = await pdfjsLib.getDocument({ data: bytes, verbosity: 0 }).promise;
  const lines = [];
  for (let p = 1; p <= doc.numPages; p++) {
    let line = "";
    for (const item of (await (await doc.getPage(p)).getTextContent()).items) {
      line += item.str;
      if (item.hasEOL) { lines.push(line); line = ""; }
    }
    if (line) lines.push(line);
  }
  return lines;
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg && msg.target === "offscreen" && msg.type === "pdf-lines") {
    pdfLines(msg.base64).then((lines) => sendResponse({ lines }), (e) => sendResponse({ error: String(e) }));
    return true;
  }
});

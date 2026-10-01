// Content script on the Car Lister page. Relays window.postMessage requests from the page to the
// extension's background worker and back. The page can only ask for the fixed actions the background
// worker implements; it cannot make arbitrary requests. To use the extension on another site address,
// add it to "content_scripts.matches" in manifest.json (and reload the extension).
const VERSION = chrome.runtime.getManifest().version;
document.documentElement.dataset.carListerExt = VERSION;

window.addEventListener("message", async (event) => {
  const msg = event.data;
  if (event.source !== window || !msg || msg.source !== "car-lister-page") return;
  let reply;
  try {
    reply = await chrome.runtime.sendMessage({ type: msg.type, payload: msg.payload });
  } catch (err) {
    reply = { error: String((err && err.message) || err) };
  }
  window.postMessage({ source: "car-lister-ext", id: msg.id, reply }, location.origin);
});

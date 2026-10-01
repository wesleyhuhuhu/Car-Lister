// Talks to the Car Lister Helper browser extension (extension/ in the repo) through its content script:
// page -> window.postMessage -> bridge.js -> background worker, and back.
let seq = 0;
const pending = new Map();

window.addEventListener("message", (event) => {
  const msg = event.data;
  if (event.source !== window || !msg || msg.source !== "car-lister-ext") return;
  const done = pending.get(msg.id);
  if (done) { pending.delete(msg.id); done(msg.reply); }
});

export function call(type, payload = {}, timeoutMs = 4000) {
  return new Promise((resolve, reject) => {
    const id = ++seq;
    const timer = setTimeout(() => { pending.delete(id); reject(new Error("The extension did not answer.")); }, timeoutMs);
    pending.set(id, (reply) => {
      clearTimeout(timer);
      if (!reply || reply.error) reject(new Error((reply && reply.error) || "The extension did not answer."));
      else resolve(reply);
    });
    window.postMessage({ source: "car-lister-page", id, type, payload }, location.origin);
  });
}

// Is the extension installed (and allowed on this site address)? -> {version, adapters} or null
export async function hello() {
  try { return await call("hello", {}, 1500); } catch { return null; }
}

// Start a background job (search / fetchOptions) and follow it until it finishes.
// onProgress(message) is called while it runs; resolves with the job's result, rejects with its message.
export async function runJob(type, payload, onProgress = () => {}) {
  const { jobId } = await call(type, payload);
  for (;;) {
    await new Promise((r) => setTimeout(r, 1200));
    const job = await call("job", { jobId });
    if (job.state === "done") return job.result;
    if (job.state === "failed" || job.state === "unknown") throw new Error(job.message || "The lookup failed.");
    onProgress(job.message || "Working…");
  }
}

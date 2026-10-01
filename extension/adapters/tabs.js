// Helpers for lookups that drive a real tab (bimmer.work, oemnavigations.com).

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// The person closed the lookup tab. Not the site's fault: it doesn't count towards the failure pause
// and doesn't trigger a fallback.
export class TabClosed extends Error {}

export async function tabExists(tabId) {
  try { await chrome.tabs.get(tabId); return true; } catch { return false; }
}

export const run = async (tabId, func, args = []) =>
  (await chrome.scripting.executeScript({ target: { tabId }, func, args }))[0].result;

// Poll fn() until it returns something truthy. Stops at once with TabClosed if the tab is closed.
// onSlow() is called once if nothing has shown up after slowMs (used to bring a background tab forward).
export async function waitFor(tabId, fn, timeoutMs, { stepMs = 500, slowMs = 3000, onSlow = null, closedMessage } = {}) {
  const start = Date.now();
  let nudged = false;
  while (Date.now() - start < timeoutMs) {
    if (!(await tabExists(tabId))) throw new TabClosed(closedMessage || "The lookup tab was closed, so the lookup was stopped.");
    try { const v = await fn(); if (v) return v; } catch { /* page still loading or navigating */ }
    if (onSlow && !nudged && Date.now() - start >= slowMs) { nudged = true; await onSlow(); }
    await sleep(stepMs);
  }
  if (!(await tabExists(tabId))) throw new TabClosed(closedMessage || "The lookup tab was closed, so the lookup was stopped.");
  return null;
}

// Chrome slows down pages in background tabs, and some only finish loading once they are shown.
// FocusSwitcher brings the lookup tab to the front when it stalls and puts the person's tab back afterwards.
export class FocusSwitcher {
  constructor(tabId) { this.tabId = tabId; this.original = null; this.switched = false; }

  async remember() {
    // The lookup tab was opened in the background, so the active tab in its window is the one the person is on.
    try {
      const { windowId } = await chrome.tabs.get(this.tabId);
      [this.original] = await chrome.tabs.query({ active: true, windowId });
    } catch { /* none */ }
    return this;
  }

  async show() {
    if (this.switched) return;
    this.switched = true;
    try { await chrome.tabs.update(this.tabId, { active: true }); } catch { /* tab gone */ }
  }

  // Back to the tab the person was on, but only if they are still looking at the lookup tab.
  async restore() {
    if (!this.switched || !this.original || this.original.id === this.tabId) return;
    try {
      const [now] = await chrome.tabs.query({ active: true, windowId: this.original.windowId });
      if (now && now.id === this.tabId) await chrome.tabs.update(this.original.id, { active: true });
    } catch { /* their tab was closed meanwhile */ }
  }
}

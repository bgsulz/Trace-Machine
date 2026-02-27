(function () {
  const storageKey = "trace-machine-tech-mode";
  const root = document.documentElement;

  function readStorage() {
    try {
      return localStorage.getItem(storageKey) === "on" ? "on" : "off";
    } catch { return "off"; }
  }

  function writeStorage(value) {
    try { localStorage.setItem(storageKey, value); } catch {}
  }

  function applyMode(mode) { root.dataset.techMode = mode; }
  function getMode() { return root.dataset.techMode || "off"; }

  function setMode(mode) {
    const next = mode === "on" ? "on" : "off";
    writeStorage(next);
    applyMode(next);
    return next;
  }

  applyMode(readStorage());
  window.__traceMachineTech = { storageKey, getMode, setMode };

  function syncButton(btn) {
    const isOn = getMode() === "on";
    btn.setAttribute("aria-pressed", isOn ? "true" : "false");
    btn.textContent = isOn ? "Technical Details: On" : "Technical Details: Off";
  }

  function initToggle() {
    const btn = document.getElementById("tech-mode-toggle");
    if (!btn) return;
    btn.addEventListener("click", () => {
      setMode(getMode() === "on" ? "off" : "on");
      syncButton(btn);
    });
    syncButton(btn);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initToggle);
  } else {
    initToggle();
  }
})();

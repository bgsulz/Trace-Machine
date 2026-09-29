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

  function syncToggles() {
    const on = getMode() === "on";
    document.querySelectorAll("input[data-tech-toggle]").forEach((toggle) => {
      toggle.checked = on;
    });
  }

  function setMode(mode) {
    const next = mode === "on" ? "on" : "off";
    writeStorage(next);
    applyMode(next);
    syncToggles();
    return next;
  }

  applyMode(readStorage());
  window.__traceMachineTech = { storageKey, getMode, setMode };

  function init() {
    document.addEventListener("change", (event) => {
      if (event.target.matches?.("input[data-tech-toggle]")) {
        setMode(event.target.checked ? "on" : "off");
      }
    });
    window.addEventListener("storage", (event) => {
      if (event.key === storageKey) {
        applyMode(readStorage());
        syncToggles();
      }
    });
    syncToggles();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();

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

  function syncCheckbox(checkbox) {
    checkbox.checked = getMode() === "on";
  }

  function initToggle() {
    const checkbox = document.getElementById("tech-mode-checkbox");
    if (!checkbox) return;
    checkbox.addEventListener("change", () => {
      setMode(checkbox.checked ? "on" : "off");
    });
    syncCheckbox(checkbox);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initToggle);
  } else {
    initToggle();
  }
})();

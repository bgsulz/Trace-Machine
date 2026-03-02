function initializeThemeButton() {
  const storageKey = "trace-machine-theme-mode";
  const root = document.documentElement;
  const validModes = ["system", "light", "dark"];
  const systemQuery = window.matchMedia?.("(prefers-color-scheme: dark)");

  function readStorage() {
    try {
      const value = localStorage.getItem(storageKey);
      return validModes.includes(value) ? value : "system";
    } catch {
      return "system";
    }
  }

  function writeStorage(value) {
    try {
      localStorage.setItem(storageKey, value);
    } catch {}
  }

  function resolveMode(mode) {
    if (mode === "system") return systemQuery?.matches ? "dark" : "light";
    return mode === "dark" ? "dark" : "light";
  }

  function applyMode(mode) {
    const resolved = resolveMode(mode);
    root.setAttribute("data-theme", resolved);
    root.dataset.themeMode = mode;
  }

  function getMode() {
    return root.dataset.themeMode || "system";
  }

  function setMode(mode) {
    const next = validModes.includes(mode) ? mode : "system";
    writeStorage(next);
    applyMode(next);
    return next;
  }

  function handleSystemChange() {
    if (getMode() === "system") applyMode("system");
  }

  applyMode(readStorage());
  window.__traceMachineTheme = { storageKey, getMode, setMode };

  if (systemQuery?.addEventListener) {
    systemQuery.addEventListener("change", handleSystemChange);
  }

  function syncRadios() {
    const mode = getMode();
    document.querySelectorAll("input[name='theme-mode']").forEach((r) => {
      r.checked = r.value === mode;
    });
  }

  function initModalControls() {
    const radios = document.querySelectorAll("input[name='theme-mode']");
    if (!radios.length) return;

    radios.forEach((r) => {
      r.addEventListener("change", () => setMode(r.value));
    });

    window.addEventListener("storage", (e) => {
      if (e.key === storageKey) {
        applyMode(readStorage());
        syncRadios();
      }
    });

    syncRadios();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initModalControls);
  } else {
    initModalControls();
  }
}

initializeThemeButton();

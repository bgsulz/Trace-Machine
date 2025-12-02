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

  function initToggle() {
    const btn = document.getElementById("theme-toggle-button");
    if (!btn) return;
    const icon = btn.querySelector("img");
    const iconMap = {
      system: btn.dataset.iconSystem,
      light: btn.dataset.iconLight,
      dark: btn.dataset.iconDark,
    };
    const order = (btn.dataset.order || "system,light,dark")
      .split(",")
      .map((m) => m.trim())
      .filter(Boolean);
    const labels = { system: "System", light: "Light", dark: "Dark" };

    function nextMode(mode) {
      const idx = order.indexOf(mode);
      return order[(idx + 1) % order.length] || "system";
    }

    function syncButton() {
      const mode = getMode();
      const label = labels[mode] || "System";
      btn.dataset.mode = mode;
      btn.setAttribute("aria-label", `Change theme (current: ${label})`);
      btn.title = `Theme: ${label}. Click to cycle.`;
      if (icon && iconMap[mode]) {
        icon.src = iconMap[mode];
        icon.alt = `${label} theme icon`;
      }
    }

    btn.addEventListener("click", () => {
      setMode(nextMode(getMode()));
      syncButton();
    });

    window.addEventListener("storage", (e) => {
      if (e.key === storageKey) {
        applyMode(readStorage());
        syncButton();
      }
    });

    syncButton();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initToggle);
  } else {
    initToggle();
  }
}

initializeThemeButton();

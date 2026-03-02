(function () {
  const storageKey = "trace-machine-hints-dismissed";

  function readDismissed() {
    try {
      return localStorage.getItem(storageKey) === "true";
    } catch {
      return false;
    }
  }

  function writeDismissed(value) {
    try {
      if (value) {
        localStorage.setItem(storageKey, "true");
      } else {
        localStorage.removeItem(storageKey);
      }
    } catch {}
  }

  function setHintsVisible(visible) {
    document.querySelectorAll("[data-hint-card]").forEach((el) => {
      el.hidden = !visible;
    });
    const checkbox = document.getElementById("hints-checkbox");
    if (checkbox) {
      checkbox.checked = visible;
    }
  }

  function init() {
    const dismissed = readDismissed();
    setHintsVisible(!dismissed);

    document.querySelectorAll("[data-hint-dismiss]").forEach((btn) => {
      btn.addEventListener("click", () => {
        writeDismissed(true);
        setHintsVisible(false);
        window.showToast("Tips hidden. Open Options to re-enable them.");
      });
    });

    const hintsCheckbox = document.getElementById("hints-checkbox");
    if (hintsCheckbox) {
      hintsCheckbox.addEventListener("change", () => {
        if (hintsCheckbox.checked) {
          writeDismissed(false);
          setHintsVisible(true);
        } else {
          writeDismissed(true);
          setHintsVisible(false);
        }
      });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();

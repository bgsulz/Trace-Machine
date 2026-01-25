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
    const navLink = document.getElementById("show-tips-link");
    if (navLink) {
      navLink.hidden = visible;
    }
  }

  function init() {
    const dismissed = readDismissed();
    setHintsVisible(!dismissed);

    document.querySelectorAll("[data-hint-dismiss]").forEach((btn) => {
      btn.addEventListener("click", () => {
        writeDismissed(true);
        setHintsVisible(false);
        window.showToast("Tips dismissed. Click 'Show tips' in the menu to see them again.");
      });
    });

    const showTipsLink = document.getElementById("show-tips-link");
    if (showTipsLink) {
      showTipsLink.addEventListener("click", (e) => {
        e.preventDefault();
        writeDismissed(false);
        setHintsVisible(true);
        window.showToast("Here are some tips to help you get started.");
      });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();

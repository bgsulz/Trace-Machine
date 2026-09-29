// Site-wide behaviour: toasts, settings dialog, report navigation helpers.
(function () {
  // ---- Toasts --------------------------------------------------------------
  const container = document.getElementById("toast-container");

  function showToast(message) {
    if (!container || !message) return;
    const toast = document.createElement("div");
    toast.className = "toast";
    toast.textContent = message;
    container.appendChild(toast);
    toast.offsetHeight; // reflow so the transition runs
    toast.classList.add("toast-visible");
    setTimeout(() => {
      toast.classList.remove("toast-visible");
      toast.addEventListener("transitionend", () => toast.remove(), { once: true });
    }, 3200);
    setTimeout(() => toast.remove(), 4000);
  }
  window.showToast = showToast;

  if (container?.dataset.initial) {
    try {
      JSON.parse(container.dataset.initial).forEach(showToast);
    } catch {}
  }
  document.body.addEventListener("showToast", (event) => showToast(event.detail?.value));

  // ---- Settings dialog -------------------------------------------------------
  const settings = document.getElementById("options-modal");
  document.getElementById("options-link")?.addEventListener("click", () => settings?.showModal());
  document.getElementById("options-modal-close")?.addEventListener("click", () => settings?.close());
  settings?.addEventListener("click", (event) => {
    if (event.target === settings) settings.close();
  });

  // ---- Report helpers ------------------------------------------------------
  // Links that point at a collapsible check open it before scrolling.
  document.addEventListener("click", (event) => {
    const link = event.target.closest?.("a[data-open-target]");
    if (!link) return;
    const target = document.getElementById(link.dataset.openTarget);
    if (target?.tagName === "DETAILS") target.open = true;
  });

  // Drag-out chips: show the visible thumbnail as the drag preview rather
  // than the transparent full-size hit area.
  document.addEventListener("dragstart", (event) => {
    const source = event.target.closest?.("[data-drag-out]");
    const thumb = source?.parentElement?.querySelector(".drag-chip__thumb");
    if (thumb && event.dataTransfer) {
      event.dataTransfer.setDragImage(thumb, thumb.width / 2, thumb.height / 2);
    }
  });

  // Copy-to-clipboard buttons: <button data-copy="text">
  document.addEventListener("click", async (event) => {
    const button = event.target.closest?.("[data-copy]");
    if (!button) return;
    try {
      await navigator.clipboard.writeText(button.dataset.copy);
      showToast(button.dataset.copyMessage || "Copied to clipboard");
    } catch {
      showToast("Couldn't copy. Your browser blocked clipboard access.");
    }
  });
})();

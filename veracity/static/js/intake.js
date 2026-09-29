// Image intake: file picker, page-wide drag & drop, paste-anywhere, and the
// "Analyzing…" overlay shown while the server runs the analyzers.
(function () {
  const IMAGE_URL = /^https?:\/\/\S+$/i;

  function isTextField(el) {
    if (!el) return false;
    if (el.isContentEditable) return true;
    const tag = el.tagName;
    if (tag === "TEXTAREA") return true;
    if (tag !== "INPUT") return false;
    return !["checkbox", "radio", "button", "submit", "file", "hidden"].includes(el.type);
  }

  // ---- Analyzing overlay -------------------------------------------------
  const overlay = document.getElementById("analyzing-overlay");
  const thumb = document.getElementById("analyzing-thumb");
  const steps = document.querySelectorAll("#analyzing-steps li");
  let stepTimer = null;
  let thumbUrl = null;

  function showAnalyzing(previewSrc) {
    if (!overlay) return;
    if (thumb) {
      if (previewSrc) {
        thumb.src = previewSrc;
        thumb.hidden = false;
        thumb.onerror = () => { thumb.hidden = true; };
      } else {
        thumb.hidden = true;
      }
    }
    let index = 0;
    const advance = () => {
      steps.forEach((li, i) => li.classList.toggle("is-active", i === index));
      if (index < steps.length - 1) index += 1;
    };
    advance();
    clearInterval(stepTimer);
    stepTimer = setInterval(advance, 900);
    overlay.hidden = false;
  }

  function hideAnalyzing() {
    if (!overlay) return;
    overlay.hidden = true;
    clearInterval(stepTimer);
    if (thumbUrl) {
      URL.revokeObjectURL(thumbUrl);
      thumbUrl = null;
    }
  }

  window.traceMachineAnalyzing = { show: showAnalyzing, hide: hideAnalyzing };

  // Returning via the back button restores the page from bfcache with the
  // overlay still visible; reset it.
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) hideAnalyzing();
  });

  // Any form or link that kicks off a server-side analysis opts in with
  // data-analyzing (sample images, crops, "analyze region", ...).
  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (event.defaultPrevented || !form.matches?.("form[data-analyzing]")) return;
    showAnalyzing(form.dataset.preview || null);
  });
  document.addEventListener("click", (event) => {
    const link = event.target.closest?.("a[data-analyzing]");
    if (!link || event.defaultPrevented || event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return;
    showAnalyzing(link.dataset.preview || null);
  });

  // ---- Intake form -------------------------------------------------------
  function init() {
    const form = document.querySelector("form[data-intake]");
    if (!form) return;
    const fileInput = form.querySelector('input[type="file"]');
    const urlInput = form.querySelector('input[name="image_url"]');
    const dropzone = document.querySelector("[data-dropzone]");
    const dropOverlay = document.getElementById("drop-overlay");

    document.querySelectorAll("[data-mod-key]").forEach((el) => {
      if (/Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent)) el.textContent = "⌘";
    });

    function submitFile(file) {
      if (!file || !file.type.startsWith("image/")) {
        window.showToast?.("That doesn't look like an image file.");
        return;
      }
      const dt = new DataTransfer();
      dt.items.add(file);
      fileInput.files = dt.files;
      if (urlInput) urlInput.value = "";
      thumbUrl = URL.createObjectURL(file);
      showAnalyzing(thumbUrl);
      form.submit();
    }

    function submitUrl(url) {
      if (!urlInput) return;
      urlInput.value = url;
      showAnalyzing(url);
      form.submit();
    }

    document.querySelectorAll("[data-file-trigger]").forEach((btn) => {
      btn.addEventListener("click", () => fileInput.click());
    });
    fileInput.addEventListener("change", () => {
      if (fileInput.files?.[0]) submitFile(fileInput.files[0]);
    });

    form.addEventListener("submit", (event) => {
      const url = (urlInput?.value || "").trim();
      if (!url && !fileInput.files?.length) {
        event.preventDefault();
        urlInput?.focus();
        return;
      }
      showAnalyzing(url || null);
    });

    // Clicking empty space in the hero drop zone opens the file picker.
    dropzone?.addEventListener("click", (event) => {
      if (event.target.closest("input, button, a, label, kbd")) return;
      fileInput.click();
    });

    // Page-wide drag and drop
    let dragDepth = 0;
    const hasFiles = (event) => Array.from(event.dataTransfer?.types || []).includes("Files");
    const setDragging = (on) => {
      if (dropzone) dropzone.classList.toggle("is-dragging", on);
      else if (dropOverlay) dropOverlay.hidden = !on;
    };

    document.addEventListener("dragenter", (event) => {
      if (!hasFiles(event)) return;
      event.preventDefault();
      dragDepth += 1;
      setDragging(true);
    });
    document.addEventListener("dragleave", (event) => {
      if (!hasFiles(event)) return;
      dragDepth = Math.max(0, dragDepth - 1);
      if (dragDepth === 0) setDragging(false);
    });
    document.addEventListener("dragover", (event) => {
      if (hasFiles(event)) event.preventDefault();
    });
    document.addEventListener("drop", (event) => {
      if (!hasFiles(event)) return;
      event.preventDefault();
      dragDepth = 0;
      setDragging(false);
      const file = event.dataTransfer.files?.[0];
      if (file) submitFile(file);
    });

    // Paste an image (screenshot, copied image) or an image link anywhere.
    document.addEventListener("paste", (event) => {
      const data = event.clipboardData;
      if (!data) return;
      for (const item of data.items || []) {
        if (item.kind === "file" && item.type.startsWith("image/")) {
          const file = item.getAsFile();
          if (file) {
            event.preventDefault();
            submitFile(file);
            return;
          }
        }
      }
      if (isTextField(document.activeElement)) return;
      const text = (data.getData("text") || "").trim();
      if (IMAGE_URL.test(text)) {
        event.preventDefault();
        submitUrl(text);
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();

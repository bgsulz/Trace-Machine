// Region selection on the result image: drag to draw a box, resize it with
// handles, or pick a suggested region, then "Analyze selection".
// Coordinates are kept normalized (0–1) so they map straight onto the crop form.
document.addEventListener("DOMContentLoaded", () => {
  const stage = document.getElementById("crop-stage");
  const image = document.getElementById("result-image");
  const box = document.getElementById("crop-selection");
  const form = document.getElementById("crop-form");
  if (!stage || !image || !box || !form) return;

  const idleBar = document.getElementById("crop-idle");
  const activeBar = document.getElementById("crop-active");
  const sizeLabel = document.getElementById("crop-size");
  const submit = document.getElementById("crop-submit");
  const startButton = document.getElementById("crop-start");
  const hint = document.getElementById("crop-hint");
  const regions = Array.from(stage.querySelectorAll(".region"));
  const MIN_PX = Number(stage.dataset.minPx) || 150;
  const MIN_NORM = 0.02; // smallest box the handles allow, as a fraction of the image
  const CLICK_SLOP = 4; // px of movement before a press becomes a drag
  const NUDGE = 0.01; // arrow-key step; Shift+arrow resizes by the same step

  let sel = null; // {l, t, r, b} in 0–1
  let gesture = null;

  const clamp = (v, lo = 0, hi = 1) => Math.min(Math.max(hi, lo), Math.max(lo, v));

  function pointFromEvent(event) {
    const rect = image.getBoundingClientRect();
    if (!rect.width || !rect.height) return null;
    return {
      x: clamp((event.clientX - rect.left) / rect.width),
      y: clamp((event.clientY - rect.top) / rect.height),
    };
  }

  function naturalSize() {
    if (!sel) return { w: 0, h: 0 };
    return {
      w: Math.round((sel.r - sel.l) * (image.naturalWidth || 0)),
      h: Math.round((sel.b - sel.t) * (image.naturalHeight || 0)),
    };
  }

  function sizeText() {
    const { w, h } = naturalSize();
    const valid = w >= MIN_PX && h >= MIN_PX;
    return { valid, text: valid ? `${w} × ${h} px` : `${w} × ${h} px · min ${MIN_PX} px` };
  }

  // `announce` is false during a drag so screen readers only hear the result.
  function render({ announce = true } = {}) {
    const active = Boolean(sel);
    const wasActive = !activeBar.hidden;
    box.hidden = !active;
    stage.classList.toggle("has-selection", active);
    stage.classList.toggle("is-cropping", active);
    idleBar.hidden = active;
    activeBar.hidden = !active;
    if (hint) hint.hidden = active;
    // Hidden regions shouldn't stay in the tab order.
    regions.forEach((region) => { region.tabIndex = active ? -1 : 0; });

    // Keep focus in the toolbar that's now visible.
    const focusLost = document.activeElement === document.body || !document.activeElement?.offsetParent;
    if (active && !wasActive && focusLost) submit.focus({ preventScroll: true });
    if (!active && wasActive && focusLost) startButton?.focus({ preventScroll: true });
    if (!active) return;

    box.style.left = `${sel.l * 100}%`;
    box.style.top = `${sel.t * 100}%`;
    box.style.width = `${(sel.r - sel.l) * 100}%`;
    box.style.height = `${(sel.b - sel.t) * 100}%`;

    const { valid, text } = sizeText();
    sizeLabel.classList.toggle("is-invalid", !valid);
    submit.disabled = !valid;
    sizeLabel.setAttribute("aria-live", announce ? "polite" : "off");
    sizeLabel.textContent = text;
  }

  function select(next, options) {
    sel = next ? { ...next } : null;
    render(options);
  }

  function boxFromRegion(el) {
    const [left, top, width, height] = (el.dataset.region || "").split(",").map(Number);
    if (![left, top, width, height].every(Number.isFinite)) return null;
    return { l: left, t: top, r: left + width, b: top + height };
  }

  function cancelGesture() {
    if (!gesture) return;
    if (stage.hasPointerCapture?.(gesture.pointerId)) stage.releasePointerCapture(gesture.pointerId);
    gesture = null;
  }

  // ---- Drawing, moving, and resizing ------------------------------------
  stage.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 || gesture) return; // one pointer at a time
    const region = event.target.closest(".region");
    const handle = event.target.closest("[data-handle]");
    const insideSelection = event.target.closest("#crop-selection");
    // Touch drags scroll the page unless a selection (or Crop) is active;
    // taps on regions still select them.
    if (event.pointerType === "touch" && !sel && !region) return;

    const start = pointFromEvent(event);
    if (!start) return;
    gesture = {
      pointerId: event.pointerId,
      mode: handle ? "resize" : insideSelection ? "move" : "draw",
      handle: handle?.dataset.handle,
      region, // a press on a region selects it unless it turns into a drag
      start,
      startSel: sel ? { ...sel } : null,
      clientX: event.clientX,
      clientY: event.clientY,
      moved: false,
    };
    stage.setPointerCapture(event.pointerId);
    event.preventDefault();
  });

  stage.addEventListener("pointermove", (event) => {
    if (!gesture || event.pointerId !== gesture.pointerId) return;
    if (!gesture.moved) {
      const dist = Math.hypot(event.clientX - gesture.clientX, event.clientY - gesture.clientY);
      if (dist < CLICK_SLOP) return;
      gesture.moved = true;
    }
    const p = pointFromEvent(event);
    if (!p) return;
    const s = gesture.startSel;

    if (gesture.mode === "draw") {
      select({
        l: Math.min(gesture.start.x, p.x),
        t: Math.min(gesture.start.y, p.y),
        r: Math.max(gesture.start.x, p.x),
        b: Math.max(gesture.start.y, p.y),
      }, { announce: false });
    } else if (gesture.mode === "move" && s) {
      const w = s.r - s.l;
      const h = s.b - s.t;
      const l = clamp(s.l + (p.x - gesture.start.x), 0, 1 - w);
      const t = clamp(s.t + (p.y - gesture.start.y), 0, 1 - h);
      select({ l, t, r: l + w, b: t + h }, { announce: false });
    } else if (gesture.mode === "resize" && s) {
      const next = { ...s };
      const hd = gesture.handle;
      if (hd.includes("w")) next.l = clamp(p.x, 0, s.r - MIN_NORM);
      if (hd.includes("e")) next.r = clamp(p.x, s.l + MIN_NORM, 1);
      if (hd.includes("n")) next.t = clamp(p.y, 0, s.b - MIN_NORM);
      if (hd.includes("s")) next.b = clamp(p.y, s.t + MIN_NORM, 1);
      select(next, { announce: false });
    }
  });

  stage.addEventListener("pointerup", (event) => {
    if (!gesture || event.pointerId !== gesture.pointerId) return;
    const { moved, mode, region } = gesture;
    cancelGesture();
    if (!moved && region) select(boxFromRegion(region));
    else if (!moved && mode === "draw") select(null); // a plain click clears
    else if (sel && (sel.r - sel.l < MIN_NORM || sel.b - sel.t < MIN_NORM)) select(null);
    else render(); // announce the final size
  });

  stage.addEventListener("pointercancel", (event) => {
    if (!gesture || event.pointerId !== gesture.pointerId) return;
    const previous = gesture.startSel;
    cancelGesture();
    select(previous);
  });

  // Keyboard: regions are buttons; Enter/Space selects them.
  stage.addEventListener("click", (event) => {
    const region = event.target.closest(".region");
    if (region && event.detail === 0) select(boxFromRegion(region));
  });

  // Arrow keys move the selection; Shift+arrows resize it.
  document.addEventListener("keydown", (event) => {
    if (!sel) return;
    if (event.key === "Escape") {
      cancelGesture();
      select(null);
      return;
    }
    const active = document.activeElement;
    const inCropUi = active === document.body || stage.contains(active) || form.contains(active);
    const dirs = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
    if (!inCropUi || !dirs[event.key] || gesture) return;
    event.preventDefault();
    const [dx, dy] = dirs[event.key].map((d) => d * NUDGE);
    const next = { ...sel };
    if (event.shiftKey) {
      next.r = clamp(sel.r + dx, sel.l + MIN_NORM, 1);
      next.b = clamp(sel.b + dy, sel.t + MIN_NORM, 1);
    } else {
      const w = sel.r - sel.l;
      const h = sel.b - sel.t;
      next.l = clamp(sel.l + dx, 0, 1 - w);
      next.t = clamp(sel.t + dy, 0, 1 - h);
      next.r = next.l + w;
      next.b = next.t + h;
    }
    select(next);
  });

  // ---- Region shortcuts ---------------------------------------------------
  document.addEventListener("click", (event) => {
    const trigger = event.target.closest("[data-select-region]");
    if (!trigger) return;
    const region = document.getElementById(trigger.dataset.selectRegion);
    if (!region) return;
    event.preventDefault();
    select(boxFromRegion(region));
    stage.scrollIntoView({ block: "nearest", behavior: "smooth" });
  });

  // Hovering a shortcut highlights the region it will select.
  document.addEventListener("pointerover", (event) => {
    const trigger = event.target.closest?.("[data-select-region]");
    document.getElementById(trigger?.dataset.selectRegion || "")?.classList.add("is-hinted");
  });
  document.addEventListener("pointerout", (event) => {
    const trigger = event.target.closest?.("[data-select-region]");
    document.getElementById(trigger?.dataset.selectRegion || "")?.classList.remove("is-hinted");
  });

  startButton?.addEventListener("click", () => select({ l: 0.1, t: 0.1, r: 0.9, b: 0.9 }));
  document.getElementById("crop-clear")?.addEventListener("click", () => select(null));

  // ---- Submit -------------------------------------------------------------
  form.addEventListener("submit", (event) => {
    if (!sel || submit.disabled) {
      event.preventDefault();
      return;
    }
    const fields = {
      crop_left: sel.l,
      crop_top: sel.t,
      crop_width: sel.r - sel.l,
      crop_height: sel.b - sel.t,
    };
    for (const [name, value] of Object.entries(fields)) {
      form.querySelector(`input[name="${name}"]`).value = value.toFixed(6);
    }
    // Disable after the browser has captured the submission, so a second
    // Enter/click can't start another analysis.
    setTimeout(() => { submit.disabled = true; }, 0);
  }, true);

  // Returning via the back button restores the page from bfcache.
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) render();
  });

  if (image.complete) render();
  else image.addEventListener("load", () => render(), { once: true });
});

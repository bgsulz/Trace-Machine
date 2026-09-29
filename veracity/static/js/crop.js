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
  const hint = document.getElementById("crop-hint");
  const MIN_PX = Number(stage.dataset.minPx) || 150;
  const MIN_NORM = 0.02; // smallest box the handles allow, as a fraction of the image
  const CLICK_SLOP = 4; // px of movement before a press becomes a drag

  let sel = null; // {l, t, r, b} in 0–1
  let gesture = null;
  let touchCropping = false; // touch drags scroll the page unless Crop was pressed

  const clamp = (v, lo = 0, hi = 1) => Math.min(hi, Math.max(lo, v));

  function pointFromEvent(event) {
    const rect = image.getBoundingClientRect();
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

  function render() {
    const active = Boolean(sel);
    box.hidden = !active;
    stage.classList.toggle("has-selection", active);
    idleBar.hidden = active;
    activeBar.hidden = !active;
    if (hint) hint.hidden = active;
    if (!active) return;

    box.style.left = `${sel.l * 100}%`;
    box.style.top = `${sel.t * 100}%`;
    box.style.width = `${(sel.r - sel.l) * 100}%`;
    box.style.height = `${(sel.b - sel.t) * 100}%`;

    const { w, h } = naturalSize();
    const valid = w >= MIN_PX && h >= MIN_PX;
    sizeLabel.textContent = valid ? `${w} × ${h} px` : `${w} × ${h} px · min ${MIN_PX} px`;
    sizeLabel.classList.toggle("is-invalid", !valid);
    submit.disabled = !valid;
  }

  function select(next) {
    sel = next ? { ...next } : null;
    if (!sel) touchCropping = false;
    stage.classList.toggle("is-cropping", Boolean(sel) || touchCropping);
    render();
  }

  function boxFromRegion(el) {
    const [left, top, width, height] = (el.dataset.region || "").split(",").map(Number);
    if (![left, top, width, height].every(Number.isFinite)) return null;
    return { l: left, t: top, r: left + width, b: top + height };
  }

  // ---- Drawing, moving, and resizing ------------------------------------
  stage.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    if (event.target.closest(".region")) return; // handled as a click
    const handle = event.target.closest("[data-handle]");
    const insideSelection = event.target.closest("#crop-selection");
    if (event.pointerType === "touch" && !touchCropping && !sel) return;

    const start = pointFromEvent(event);
    gesture = {
      mode: handle ? "resize" : insideSelection ? "move" : "draw",
      handle: handle?.dataset.handle,
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
    if (!gesture) return;
    if (!gesture.moved) {
      const dist = Math.hypot(event.clientX - gesture.clientX, event.clientY - gesture.clientY);
      if (dist < CLICK_SLOP) return;
      gesture.moved = true;
    }
    const p = pointFromEvent(event);
    const s = gesture.startSel;

    if (gesture.mode === "draw") {
      select({
        l: Math.min(gesture.start.x, p.x),
        t: Math.min(gesture.start.y, p.y),
        r: Math.max(gesture.start.x, p.x),
        b: Math.max(gesture.start.y, p.y),
      });
    } else if (gesture.mode === "move" && s) {
      const w = s.r - s.l;
      const h = s.b - s.t;
      const l = clamp(s.l + (p.x - gesture.start.x), 0, 1 - w);
      const t = clamp(s.t + (p.y - gesture.start.y), 0, 1 - h);
      select({ l, t, r: l + w, b: t + h });
    } else if (gesture.mode === "resize" && s) {
      const next = { ...s };
      const hd = gesture.handle;
      if (hd.includes("w")) next.l = clamp(p.x, 0, s.r - MIN_NORM);
      if (hd.includes("e")) next.r = clamp(p.x, s.l + MIN_NORM, 1);
      if (hd.includes("n")) next.t = clamp(p.y, 0, s.b - MIN_NORM);
      if (hd.includes("s")) next.b = clamp(p.y, s.t + MIN_NORM, 1);
      select(next);
    }
  });

  function endGesture() {
    if (!gesture) return;
    // A plain click on the image (not the selection) clears the selection.
    if (!gesture.moved && gesture.mode === "draw") select(null);
    gesture = null;
  }
  stage.addEventListener("pointerup", endGesture);
  stage.addEventListener("pointercancel", endGesture);

  // ---- Region shortcuts ---------------------------------------------------
  stage.addEventListener("click", (event) => {
    const region = event.target.closest(".region");
    if (region) select(boxFromRegion(region));
  });

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

  document.getElementById("crop-start")?.addEventListener("click", () => {
    touchCropping = true;
    select({ l: 0.1, t: 0.1, r: 0.9, b: 0.9 });
  });
  document.getElementById("crop-clear")?.addEventListener("click", () => select(null));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && sel) select(null);
  });

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
  }, true);

  if (image.complete) render();
  else image.addEventListener("load", render, { once: true });
});

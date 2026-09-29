// Manual crop & re-analyze on the result page (Cropper.js).
document.addEventListener("DOMContentLoaded", () => {
  const image = document.getElementById("result-image");
  const form = document.getElementById("crop-form");
  const toggle = document.getElementById("crop-toggle");
  const cancel = document.getElementById("crop-cancel");
  const controls = document.getElementById("crop-active-controls");
  const submit = document.getElementById("crop-submit");
  const status = document.getElementById("crop-status");
  const toolbar = form?.querySelector(".media-toolbar");
  if (!image || !form || !toggle || !controls || !toolbar || typeof Cropper === "undefined") return;

  const MIN_CROP_PX = 150;
  let cropper = null;

  const setActive = (active) => {
    toolbar.dataset.state = active ? "active" : "idle";
    controls.hidden = !active;
    toolbar.querySelectorAll(":scope > .btn").forEach((btn) => { btn.hidden = active; });
    if (!active && status) status.hidden = true;
  };

  const validate = () => {
    if (!cropper || !submit || !status) return;
    const data = cropper.getData(true);
    const valid = data.width >= MIN_CROP_PX && data.height >= MIN_CROP_PX;
    submit.disabled = !valid;
    status.hidden = valid;
    status.textContent = valid ? "" : `Selection too small (${MIN_CROP_PX}×${MIN_CROP_PX} px minimum)`;
  };

  const destroy = () => {
    cropper?.destroy();
    cropper = null;
    setActive(false);
  };

  toggle.addEventListener("click", () => {
    if (cropper) return;
    cropper = new Cropper(image, {
      viewMode: 1,
      background: false,
      autoCropArea: 1,
      movable: false,
      zoomOnWheel: false,
      ready() {
        if (image.naturalWidth && image.naturalHeight) {
          cropper.setData({ x: 0, y: 0, width: image.naturalWidth, height: image.naturalHeight });
        }
        validate();
      },
      crop: validate,
    });
    setActive(true);
  });

  cancel?.addEventListener("click", destroy);

  form.addEventListener("submit", (event) => {
    // The auto-crop button shares this form but posts elsewhere.
    if (event.submitter?.hasAttribute("data-autocrop")) return;
    if (!cropper) {
      event.preventDefault();
      return;
    }
    const data = cropper.getData(true);
    const natW = image.naturalWidth || 1;
    const natH = image.naturalHeight || 1;
    const fields = {
      crop_left: data.x / natW,
      crop_top: data.y / natH,
      crop_width: data.width / natW,
      crop_height: data.height / natH,
    };
    for (const [name, value] of Object.entries(fields)) {
      const input = form.querySelector(`input[name="${name}"]`);
      if (input) input.value = Number.isFinite(value) ? value.toFixed(6) : 0;
    }
    destroy();
  }, true);
});

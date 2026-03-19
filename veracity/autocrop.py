"""Auto-crop detection for social media overlays.

Uses Sobel Y-gradient row scanning to detect text banners at image edges.
A text banner is identified when the bottom (or top) portion of an image
contains a row with an average absolute Y-gradient significantly higher
than the image's interior AND that high-gradient zone spans at least
80% of the image width.

The crop line is taken as the *topmost* qualifying row in the bottom zone
(or *bottommost* qualifying row in the top zone), which corresponds to the
boundary between image content and the overlay banner.
"""
from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO

import cv2
import numpy as np
from PIL import Image, ImageOps


# Minimum fraction of width that a qualifying row must cover.
_MIN_WIDTH_COVERAGE = 0.80
# Maximum fraction of image height that a detected banner may occupy.
_MAX_BANNER_HEIGHT_FRAC = 0.30
# Minimum banner height in rows (avoids single-pixel edge artifacts).
_MIN_BANNER_ROWS = 8
# Multiplier over baseline std used to set the spike threshold.
_THRESHOLD_STD_FACTOR = 2.5
# Absolute floor for the spike threshold (gradient units).
_THRESHOLD_FLOOR = 5.0
# Minimum confidence value required to expose the button to the user.
CONFIDENCE_THRESHOLD = 0.50


@dataclass
class OverlayCropResult:
    has_overlay: bool
    confidence: float           # 0-1; how strongly the banner signal stands out
    crop_box: tuple[float, float, float, float] | None  # (left, top, w, h) normalized
    method: str                 # human-readable description of what was found


def detect_overlay_crop(image_bytes: bytes) -> OverlayCropResult:
    """Detect social-media text banners and return a suggested crop.

    Runs Sobel Y-gradient row scanning in both the top and bottom edge zones.
    Returns an :class:`OverlayCropResult` describing whether a croppable
    overlay was found and, if so, the normalized crop box that removes it.
    """
    try:
        gray = _load_gray(image_bytes)
    except Exception:
        return OverlayCropResult(has_overlay=False, confidence=0.0, crop_box=None, method="")

    H, W = gray.shape
    if H < 40 or W < 40:
        return OverlayCropResult(has_overlay=False, confidence=0.0, crop_box=None, method="")

    abs_sobel = _abs_sobel_y(gray)
    row_means = abs_sobel.mean(axis=1)  # shape: (H,)

    # Characterise interior gradient to build a relative threshold.
    center_lo = int(H * 0.20)
    center_hi = int(H * 0.80)
    interior = row_means[center_lo:center_hi]
    if interior.size == 0:
        return OverlayCropResult(has_overlay=False, confidence=0.0, crop_box=None, method="")

    baseline = float(np.median(interior))
    std = float(interior.std())
    threshold = max(_THRESHOLD_FLOOR, baseline + _THRESHOLD_STD_FACTOR * std)

    bottom = _scan_banner(row_means, abs_sobel, H, W, threshold, from_bottom=True)
    top = _scan_banner(row_means, abs_sobel, H, W, threshold, from_bottom=False)

    if bottom is None and top is None:
        return OverlayCropResult(has_overlay=False, confidence=0.0, crop_box=None, method="")

    top_frac = top[0] if top is not None else 0.0
    top_conf = top[1] if top is not None else 0.0
    bottom_frac = bottom[0] if bottom is not None else 1.0
    bottom_conf = bottom[1] if bottom is not None else 0.0

    h = bottom_frac - top_frac
    if h <= 0.10:
        return OverlayCropResult(has_overlay=False, confidence=0.0, crop_box=None, method="")

    confidence = max(top_conf, bottom_conf)
    if confidence < CONFIDENCE_THRESHOLD:
        return OverlayCropResult(has_overlay=False, confidence=confidence, crop_box=None, method="")

    parts = []
    if top is not None:
        parts.append("top text banner")
    if bottom is not None:
        parts.append("bottom text banner")
    method = "Detected: " + " and ".join(parts)

    return OverlayCropResult(
        has_overlay=True,
        confidence=confidence,
        crop_box=(0.0, top_frac, 1.0, h),
        method=method,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_gray(image_bytes: bytes) -> np.ndarray:
    with Image.open(BytesIO(image_bytes)) as img:
        img = ImageOps.exif_transpose(img)
        img = img.convert("L")
        return np.array(img, dtype=np.uint8)


def _abs_sobel_y(gray: np.ndarray) -> np.ndarray:
    sobel = cv2.Sobel(gray.astype(np.float32), cv2.CV_32F, 0, 1, ksize=3)
    return np.abs(sobel)


def _scan_banner(
    row_means: np.ndarray,
    abs_sobel: np.ndarray,
    H: int,
    W: int,
    threshold: float,
    *,
    from_bottom: bool,
) -> tuple[float, float] | None:
    """Scan for a text banner at one edge.

    Searches the top or bottom ``_MAX_BANNER_HEIGHT_FRAC`` of the image for
    rows that simultaneously have above-threshold average gradient AND span
    at least ``_MIN_WIDTH_COVERAGE`` of the image width.  The crop line is
    the *topmost* such row for a bottom banner (border with image content)
    or the *bottommost* such row for a top banner.

    Returns ``(normalized_crop_line, confidence)`` or ``None``.

    * ``from_bottom=True``:  banner occupies rows ``[crop_row, H)``;
      crop box keeps ``[0, crop_row)`` → crop_line = ``crop_row / H``.
    * ``from_bottom=False``: banner occupies rows ``[0, crop_row]``;
      crop box keeps ``(crop_row, H)`` → crop_line = ``(crop_row + 1) / H``.
    """
    # Define the edge zone to search.
    if from_bottom:
        zone_lo = int(H * (1.0 - _MAX_BANNER_HEIGHT_FRAC))
        zone_hi = H
    else:
        zone_lo = 0
        zone_hi = int(H * _MAX_BANNER_HEIGHT_FRAC) + 1

    col_threshold = threshold * 0.30

    # Collect all rows in the zone that exceed the gradient threshold AND
    # have sufficient width coverage. We look for the row nearest the image
    # content (topmost for a bottom banner; bottommost for a top banner).
    crop_row: int | None = None

    for r in range(zone_lo, zone_hi):
        if row_means[r] <= threshold:
            continue
        row_grad = abs_sobel[r, :]
        if float((row_grad > col_threshold).mean()) < _MIN_WIDTH_COVERAGE:
            continue
        # This row qualifies. For bottom banners we want the smallest index
        # (topmost); for top banners the largest index (bottommost).
        if from_bottom:
            if crop_row is None or r < crop_row:
                crop_row = r
        else:
            if crop_row is None or r > crop_row:
                crop_row = r

    if crop_row is None:
        return None

    # Validate: the resulting banner region must be tall enough to be real.
    banner_height = (H - crop_row) if from_bottom else (crop_row + 1)
    if banner_height < _MIN_BANNER_ROWS:
        return None

    # Confidence: how strongly the boundary row's gradient exceeds the
    # threshold.  A text banner on a black/solid background produces a very
    # prominent edge at the boundary even though the *average* gradient of
    # the entire banner region may be low (mostly solid background).
    boundary_strength = float(row_means[crop_row])
    raw_ratio = (boundary_strength - threshold) / (threshold + 1e-6)
    confidence = min(1.0, max(0.0, raw_ratio))

    if from_bottom:
        return (crop_row / H, confidence)
    else:
        return ((crop_row + 1) / H, confidence)

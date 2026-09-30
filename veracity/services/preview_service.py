"""Browser-displayable renditions of analyzed images.

Most uploads are served as-is. Formats browsers can't show (HEIC, TIFF, ...)
are converted once to JPEG and cached beside the analysis, so repeated views
don't re-decode a potentially huge image.
"""

from __future__ import annotations

import os
import tempfile
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

from ..analysis_cache import analysis_dir


class PreviewError(Exception):
    """The image couldn't be converted for display."""


def preview_path(analysis_id: str) -> Path:
    return analysis_dir() / f"{analysis_id}-preview.jpg"


def cached_display_jpeg(analysis_id: str, image_bytes: bytes) -> bytes:
    """Return a JPEG rendition, converting and caching it on first use."""
    path = preview_path(analysis_id)
    try:
        return path.read_bytes()
    except FileNotFoundError:
        pass

    data = render_display_jpeg(image_bytes)
    # Write atomically so concurrent requests never read a partial file.
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except OSError:
        Path(tmp).unlink(missing_ok=True)
    return data


def render_display_jpeg(image_bytes: bytes) -> bytes:
    """Convert any Pillow-readable image to an sRGB-ish display JPEG.

    Applies EXIF orientation (matching how crops are computed), keeps the ICC
    profile so wide-gamut photos (iPhone Display P3) keep their colours,
    scales 16-bit images down instead of clipping, and flattens transparency
    onto white.
    """
    try:
        with Image.open(BytesIO(image_bytes)) as img:
            icc_profile = img.info.get("icc_profile")
            img.load()
            ImageOps.exif_transpose(img, in_place=True)
            img = _to_rgb(img)
            buffer = BytesIO()
            img.save(buffer, format="JPEG", quality=90, icc_profile=icc_profile)
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise PreviewError(str(exc)) from exc
    return buffer.getvalue()


def _to_rgb(img: Image.Image) -> Image.Image:
    if img.mode in ("I;16", "I;16B", "I;16L", "I;16N", "I"):
        # 16/32-bit grayscale: scale to 8 bits rather than clipping to white.
        arr = np.asarray(img)
        peak = 65535 if img.mode.startswith("I;16") else max(int(arr.max()), 1)
        img = Image.fromarray((arr.astype(np.float64) * (255 / peak)).clip(0, 255).astype(np.uint8))

    has_alpha = img.mode in ("RGBA", "LA", "PA") or (
        img.mode == "P" and "transparency" in img.info
    )
    if has_alpha:
        rgba = img.convert("RGBA")
        flattened = Image.new("RGB", rgba.size, (255, 255, 255))
        flattened.paste(rgba, mask=rgba.getchannel("A"))
        return flattened
    if img.mode != "RGB":
        return img.convert("RGB")
    return img

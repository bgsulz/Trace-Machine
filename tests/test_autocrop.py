"""Tests for the auto-crop overlay detection feature."""
import io
import json
import re

import numpy as np
import pytest
from PIL import Image, ImageDraw

from veracity.autocrop import CONFIDENCE_THRESHOLD, detect_overlay_crop


# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------

def _make_banner_image(
    width: int = 400,
    height: int = 300,
    *,
    banner_bottom: int = 0,
    banner_top: int = 0,
    fmt: str = "PNG",
    gradient_background: bool = False,
) -> bytes:
    """Image with optional full-width text banners at the edges.

    When ``gradient_background=True`` the background is a smooth vertical
    gradient, giving the crop region non-trivial entropy/contrast (needed by
    the crop validator) while keeping the interior Sobel-Y gradient low and
    predictable (needed for banner detection to work reliably).
    """
    content_h = height - banner_bottom - banner_top

    if gradient_background and content_h > 0:
        arr = np.zeros((height, width, 3), dtype=np.uint8)
        # Smooth vertical gradient for the image-content region.
        vals = np.linspace(80, 200, content_h, dtype=np.uint8)
        for i, v in enumerate(vals):
            arr[banner_top + i, :] = [v, v, v]
        img = Image.fromarray(arr)
    else:
        img = Image.new("RGB", (width, height), color=(200, 150, 100))

    draw = ImageDraw.Draw(img)

    if banner_bottom > 0:
        y0 = height - banner_bottom
        draw.rectangle([0, y0, width, height], fill=(20, 20, 20))
        for x in range(10, width - 10, 30):
            draw.rectangle([x, y0 + 5, x + 20, y0 + banner_bottom - 5], fill=(240, 240, 240))

    if banner_top > 0:
        draw.rectangle([0, 0, width, banner_top], fill=(20, 20, 20))
        for x in range(10, width - 10, 30):
            draw.rectangle([x, 5, x + 20, banner_top - 5], fill=(240, 240, 240))

    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


def _make_narrow_overlay_image() -> bytes:
    """Image with a high-contrast banner that spans only 50% of the width."""
    img = Image.new("RGB", (400, 300), color=(200, 150, 100))
    draw = ImageDraw.Draw(img)
    draw.rectangle([100, 240, 300, 300], fill=(20, 20, 20))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_noise_image(width: int = 300, height: int = 300) -> bytes:
    rng = np.random.default_rng(0)
    arr = rng.integers(0, 256, (height, width, 3), dtype=np.uint8)
    img = Image.fromarray(arr)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _extract_analysis_id(html: str) -> str:
    match = re.search(r"/analysis/([a-f0-9]+)/analyzers", html)
    assert match, "analysis_id not found in response body"
    return match.group(1)


def _upload_banner_image(client, banner_bottom: int = 60, gradient: bool = False) -> tuple[str, bytes]:
    image_bytes = _make_banner_image(banner_bottom=banner_bottom, gradient_background=gradient)
    data = {"file": (io.BytesIO(image_bytes), "banner.png"), "image_url": ""}
    resp = client.post("/analyze", data=data, content_type="multipart/form-data")
    assert resp.status_code == 200
    analysis_id = _extract_analysis_id(resp.data.decode("utf-8"))
    return analysis_id, image_bytes


# ===========================================================================
# Unit tests: detect_overlay_crop
# ===========================================================================

class TestDetectOverlayCropUnit:
    def test_detects_bottom_banner(self):
        result = detect_overlay_crop(_make_banner_image(banner_bottom=60))
        assert result.has_overlay
        assert result.confidence >= CONFIDENCE_THRESHOLD
        assert result.crop_box is not None
        left, top, w, h = result.crop_box
        assert left == pytest.approx(0.0)
        assert top == pytest.approx(0.0)
        assert w == pytest.approx(1.0)
        # Banner is 60/300 = 20% of height; we keep the top ~80%.
        assert h == pytest.approx(1.0 - 60 / 300, abs=0.02)
        assert "bottom text banner" in result.method

    def test_detects_top_banner(self):
        result = detect_overlay_crop(_make_banner_image(banner_top=50))
        assert result.has_overlay
        assert result.crop_box is not None
        _left, top, w, h = result.crop_box
        assert top == pytest.approx(50 / 300, abs=0.02)
        assert h == pytest.approx(1.0 - 50 / 300, abs=0.02)
        assert "top text banner" in result.method

    def test_detects_both_banners(self):
        result = detect_overlay_crop(_make_banner_image(banner_bottom=50, banner_top=40))
        assert result.has_overlay
        assert result.crop_box is not None
        _left, top, w, h = result.crop_box
        assert top == pytest.approx(40 / 300, abs=0.02)
        bottom = top + h
        assert bottom == pytest.approx(1.0 - 50 / 300, abs=0.02)
        assert "top text banner" in result.method
        assert "bottom text banner" in result.method

    def test_clean_image_has_no_overlay(self):
        result = detect_overlay_crop(_make_banner_image())
        assert not result.has_overlay
        assert result.crop_box is None

    def test_rejects_banner_taller_than_25_percent(self):
        # 110 px out of 300 = ~37% — exceeds _MAX_BANNER_HEIGHT_FRAC.
        result = detect_overlay_crop(_make_banner_image(banner_bottom=110))
        assert not result.has_overlay

    def test_rejects_narrow_overlay(self):
        # Banner only covers 50% of width — fails _MIN_WIDTH_COVERAGE.
        result = detect_overlay_crop(_make_narrow_overlay_image())
        assert not result.has_overlay

    def test_noise_image_has_no_overlay(self):
        # Random pixel noise should never look like a text banner.
        result = detect_overlay_crop(_make_noise_image())
        assert not result.has_overlay

    def test_invalid_bytes_returns_no_overlay(self):
        result = detect_overlay_crop(b"not an image")
        assert not result.has_overlay
        assert result.crop_box is None

    def test_crop_box_is_normalized(self):
        result = detect_overlay_crop(_make_banner_image(banner_bottom=60))
        assert result.has_overlay
        left, top, w, h = result.crop_box
        assert 0.0 <= left <= 1.0
        assert 0.0 <= top <= 1.0
        assert 0.0 < w <= 1.0
        assert 0.0 < h <= 1.0
        assert left + w <= 1.0 + 1e-6
        assert top + h <= 1.0 + 1e-6


# ===========================================================================
# Integration tests: /analysis/<id>/autocrop route
# ===========================================================================

class TestAutocropRoute:
    def test_autocrop_creates_containment_and_reruns_analysis(self, client, app):
        # gradient_background gives non-trivial entropy (crop validator) while
        # keeping the interior Sobel-Y low enough for banner detection to fire.
        analysis_id, _ = _upload_banner_image(client, banner_bottom=60, gradient=True)

        resp = client.post(f"/analysis/{analysis_id}/autocrop")
        assert resp.status_code == 200
        assert b"Provenance Report" in resp.data

        from veracity.models import ImageContainment

        with app.app_context():
            links = ImageContainment.query.all()
            assert len(links) >= 1
            latest = links[-1]
            crop_box = json.loads(latest.crop_box_json)
            # Cropped image should be narrower in height than the original.
            # crop_box is [left, top, width, height]; height should be < 1.0.
            assert crop_box[3] < 1.0

    def test_autocrop_on_clean_image_flashes_and_rerenders(self, client):
        # Upload a clean image (no banner) — autocrop should gracefully decline.
        clean_bytes = _make_banner_image()  # no banner
        data = {"file": (io.BytesIO(clean_bytes), "clean.png"), "image_url": ""}
        resp = client.post("/analyze", data=data, content_type="multipart/form-data")
        assert resp.status_code == 200
        analysis_id = _extract_analysis_id(resp.data.decode("utf-8"))

        resp2 = client.post(f"/analysis/{analysis_id}/autocrop")
        assert resp2.status_code == 200
        # Should still render the result page, just with a flash message.
        assert b"Provenance Report" in resp2.data

    def test_autocrop_on_expired_analysis_returns_gone(self, client):
        resp = client.post("/analysis/deadbeef00000000/autocrop")
        # The app returns 410 Gone for expired analyses.
        assert resp.status_code == 410

    def test_autocrop_button_present_for_banner_image(self, client):
        resp = client.post(
            "/analyze",
            data={
                "file": (io.BytesIO(_make_banner_image(banner_bottom=60)), "banner.png"),
                "image_url": "",
            },
            content_type="multipart/form-data",
        )
        assert resp.status_code == 200
        body = resp.data.decode("utf-8")
        assert "Auto-crop to image" in body

    def test_autocrop_button_absent_for_clean_image(self, client):
        clean_bytes = _make_banner_image()  # no banner
        data = {"file": (io.BytesIO(clean_bytes), "clean.png"), "image_url": ""}
        resp = client.post("/analyze", data=data, content_type="multipart/form-data")
        assert resp.status_code == 200
        body = resp.data.decode("utf-8")
        assert "Auto-crop to image" not in body

    def test_autocrop_button_absent_after_autocrop(self, client):
        # After an auto-crop, the result should not offer another auto-crop.
        analysis_id, _ = _upload_banner_image(client, banner_bottom=60, gradient=True)
        resp = client.post(f"/analysis/{analysis_id}/autocrop")
        assert resp.status_code == 200
        body = resp.data.decode("utf-8")
        assert "Auto-crop to image" not in body

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
    resp = client.post("/analyze", data=data, content_type="multipart/form-data", follow_redirects=True)
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

    def test_rejects_banner_taller_than_30_percent(self):
        # 120 px out of 300 = 40% — exceeds _MAX_BANNER_HEIGHT_FRAC (0.30).
        result = detect_overlay_crop(_make_banner_image(banner_bottom=120))
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
# Integration tests: suggested crop regions on the result page
# ===========================================================================

def _suggested_box(body: str) -> list[float] | None:
    match = re.search(r'id="region-suggested"[^>]*data-region="([^"]+)"', body, re.S)
    return [float(v) for v in match.group(1).split(",")] if match else None


class TestSuggestedCrop:
    def test_suggestion_shown_as_selectable_region_for_banner_image(self, client):
        resp = client.post(
            "/analyze",
            data={
                "file": (io.BytesIO(_make_banner_image(banner_bottom=60)), "banner.png"),
                "image_url": "",
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        assert resp.status_code == 200
        body = resp.data.decode("utf-8")
        box = _suggested_box(body)
        assert box is not None
        assert box[3] < 1.0  # the banner is excluded
        assert 'data-select-region="region-suggested"' in body
        assert "Use suggested crop" in body

    def test_no_suggestion_for_clean_image(self, client):
        data = {"file": (io.BytesIO(_make_banner_image()), "clean.png"), "image_url": ""}
        resp = client.post("/analyze", data=data, content_type="multipart/form-data", follow_redirects=True)
        assert resp.status_code == 200
        body = resp.data.decode("utf-8")
        assert _suggested_box(body) is None
        assert "Use suggested crop" not in body

    def test_cropping_to_suggestion_links_containment(self, client, app):
        # gradient_background gives non-trivial entropy (crop validator) while
        # keeping the interior Sobel-Y low enough for banner detection to fire.
        analysis_id, _ = _upload_banner_image(client, banner_bottom=60, gradient=True)
        page = client.get(f"/analysis/{analysis_id}").data.decode("utf-8")
        left, top, width, height = _suggested_box(page)

        resp = client.post(
            f"/analysis/{analysis_id}/crop",
            data={"crop_left": left, "crop_top": top, "crop_width": width, "crop_height": height},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        body = resp.data.decode("utf-8")
        assert "Provenance Report" in body
        # A cropped result doesn't suggest cropping again.
        assert _suggested_box(body) is None

        from veracity.models import ImageContainment

        with app.app_context():
            links = ImageContainment.query.all()
            assert len(links) >= 1
            assert json.loads(links[-1].crop_box_json)[3] < 1.0

    def test_autocrop_route_is_retired(self, client):
        analysis_id, _ = _upload_banner_image(client, banner_bottom=60, gradient=True)
        resp = client.post(f"/analysis/{analysis_id}/autocrop")
        assert resp.status_code in (404, 405)

    def test_analyzed_suggestion_becomes_contained_region(self, client, app):
        analysis_id, _ = _upload_banner_image(client, banner_bottom=60, gradient=True)
        page = client.get(f"/analysis/{analysis_id}").data.decode("utf-8")
        left, top, width, height = _suggested_box(page)

        crop = client.post(
            f"/analysis/{analysis_id}/crop",
            data={"crop_left": left, "crop_top": top, "crop_width": width, "crop_height": height},
        )
        child_id = crop.headers["Location"].rsplit("/", 1)[-1]

        from veracity.analysis_cache import load_analysis_metadata

        with app.app_context():
            child_phash = load_analysis_metadata(child_id)["phash"]
        # Regions are only shown once they carry evidence, e.g. a vote.
        client.post("/vote", data={"phash": child_phash, "vote": "ai"})

        body = client.get(f"/analysis/{analysis_id}").data.decode("utf-8")
        assert 'id="region-contained-1"' in body
        assert 'data-select-region="region-contained-1"' in body
        # The suggestion is now covered by the contained region.
        assert _suggested_box(body) is None


def test_same_box_tolerance():
    from veracity.web.ui import same_box

    assert same_box([0, 0.2216, 1, 0.4783], (0.0, 0.221667, 1.0, 0.478333))
    assert not same_box([0, 0.2, 1, 0.5], [0, 0.3, 1, 0.5])
    assert not same_box(None, [0, 0, 1, 1])

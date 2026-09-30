"""Adobe TrustMark decoding (ONNX, no PyTorch).

Fixtures in tests/fixtures/trustmark_{Q,P}.jpg were watermarked with Adobe's
official PyTorch encoder (payload below, BCH_4), so the real-model tests prove
compatibility with production marks, not just with our own round trip.
The real-model tests need the ~95 MB decoders; they run when they're present
(TRUSTMARK_TEST_MODEL_DIR, or after `flask download-models`) and skip otherwise.
"""

import os
from pathlib import Path

import pytest
from PIL import Image

from veracity import _parse_invisible_watermark_decoders
from veracity.watermarks.trustmark import (
    MODEL_CHECKSUMS,
    ModelUnavailable,
    TrustMarkDecoder,
    ensure_model,
)

FIXTURES = Path(__file__).parent / "fixtures"
PAYLOAD = ("1011001110001111" * 8)[:68]


def _model_dir() -> Path | None:
    candidates = [
        os.environ.get("TRUSTMARK_TEST_MODEL_DIR"),
        Path(__file__).resolve().parents[1] / "instance" / "models" / "trustmark",
    ]
    for candidate in candidates:
        if candidate and all((Path(candidate) / name).is_file() for name in MODEL_CHECKSUMS):
            return Path(candidate)
    return None


needs_models = pytest.mark.skipif(_model_dir() is None, reason="TrustMark models not installed")


# --- Confirmation logic (no models needed) --------------------------------


class _ScriptedDecoder(TrustMarkDecoder):
    """Returns scripted raw decodes: first call is the original image."""

    def __init__(self, results):
        super().__init__(Path("."), download=False)
        self._results = iter(results)

    def _decode_variant(self, image, variant):
        return next(self._results)


def test_chance_decode_that_doesnt_survive_edits_is_rejected():
    raw = ("0101", 3)
    # Q: original decodes, all three perturbed copies don't; P: nothing.
    decoder = _ScriptedDecoder([raw, None, None, None, None])
    assert decoder.decode(Image.new("RGB", (64, 64))) is None


def test_mark_confirmed_by_perturbed_copies_is_reported():
    raw = ("0101", 2)
    decoder = _ScriptedDecoder([raw, raw, None, raw])
    hit = decoder.decode(Image.new("RGB", (64, 64)))
    assert hit is not None
    assert (hit.variant, hit.schema, hit.payload, hit.confirmations) == ("Q", 2, "0101", 2)


def test_missing_model_without_download_is_unavailable(tmp_path):
    with pytest.raises(ModelUnavailable):
        ensure_model(tmp_path, "Q", download=False)


def test_trustmark_is_on_by_default_and_can_be_turned_off():
    import logging

    log = logging.getLogger("test")
    assert _parse_invisible_watermark_decoders("", logger=log) == {"adobe_trustmark"}
    assert _parse_invisible_watermark_decoders("none", logger=log) == set()
    assert _parse_invisible_watermark_decoders("imwatermark", logger=log) == {"open_dwt_dct"}


# --- Real models -------------------------------------------------------------


@needs_models
@pytest.mark.parametrize("variant", ["Q", "P"])
def test_official_marks_decode_with_exact_payload(variant):
    decoder = TrustMarkDecoder(_model_dir(), download=False)
    hit = decoder.decode(Image.open(FIXTURES / f"trustmark_{variant}.jpg"))
    assert hit is not None
    assert hit.variant == variant
    assert hit.payload == PAYLOAD
    assert hit.confirmations >= 2


@needs_models
def test_unmarked_image_has_no_mark():
    decoder = TrustMarkDecoder(_model_dir(), download=False)
    assert decoder.decode(Image.open(FIXTURES / "trustmark_none.jpg")) is None


def _run_invisible(app, image_bytes, **config):
    """Run the real analyzer path (context + manager) with temporary config."""
    from veracity.analyzers.manager import run_single_analyzer
    from veracity.registry import prepare_analysis_context

    saved = {k: app.config.get(k) for k in config}
    app.config.update(config)
    try:
        with app.app_context():
            return run_single_analyzer(prepare_analysis_context(image_bytes), "invisible")
    finally:
        app.config.update(saved)


@needs_models
def test_invisible_analyzer_reports_trustmark(app):
    result = _run_invisible(
        app,
        (FIXTURES / "trustmark_Q.jpg").read_bytes(),
        INVISIBLE_WATERMARK_DECODERS={"adobe_trustmark"},
        TRUSTMARK_MODEL_DIR=str(_model_dir()),
    )
    assert result["status"] == "FOUND"
    finding = result["data"]["findings"][0]
    assert finding["label"] == "Adobe TrustMark"
    assert "Durable Content Credentials" in finding["note"]
    assert len(finding["payload"]) == 17  # 68 bits -> 17 hex digits, zero-padded


def test_missing_models_show_as_unavailable_without_blocking(app, tmp_path):
    result = _run_invisible(
        app,
        (FIXTURES / "trustmark_none.jpg").read_bytes(),
        INVISIBLE_WATERMARK_DECODERS={"adobe_trustmark"},
        TRUSTMARK_MODEL_DIR=str(tmp_path),
        TRUSTMARK_AUTO_DOWNLOAD=False,
    )
    assert result["status"] == "NOT AVAILABLE"


def test_missing_model_downloads_in_the_background(tmp_path, monkeypatch):
    import threading
    import veracity.watermarks.trustmark as trustmark

    started = threading.Event()
    release = threading.Event()
    real_ensure = trustmark.ensure_model

    def slow_ensure(model_dir, variant, *, download):
        if not download:
            return real_ensure(model_dir, variant, download=False)
        started.set()
        release.wait(5)  # a slow download
        raise trustmark.ModelUnavailable("offline")

    monkeypatch.setattr(trustmark, "ensure_model", slow_ensure)
    decoder = TrustMarkDecoder(tmp_path, download=True)
    with pytest.raises(ModelUnavailable, match="downloading"):
        decoder.decode(Image.open(FIXTURES / "trustmark_none.jpg"))  # returns immediately
    assert started.wait(2)
    release.set()
    decoder._download_thread.join(5)
    # After a failure it backs off instead of retrying on every request.
    assert decoder._download_failed_at > 0
    decoder._start_background_download()
    assert not decoder._download_thread.is_alive()


@needs_models
def test_concurrent_decodes_are_consistent():
    """The shared error-correction state must not corrupt parallel decodes."""
    import sys
    from concurrent.futures import ThreadPoolExecutor

    decoder = TrustMarkDecoder(_model_dir(), download=False)
    images = [Image.open(FIXTURES / name).convert("RGB") for name in ("trustmark_Q.jpg", "trustmark_P.jpg")]
    expected = [decoder.decode(img) for img in images]
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)  # maximize thread interleaving
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda i: decoder.decode(images[i % 2]), range(32)))
    finally:
        sys.setswitchinterval(old)
    assert results == [expected[i % 2] for i in range(32)]


def test_positive_int_settings_fall_back_safely():
    import logging

    from veracity import _positive_int

    log = logging.getLogger("test")
    assert _positive_int(None, default=2, logger=log) == 2
    assert _positive_int("4", default=2, logger=log) == 4
    assert _positive_int("abc", default=2, logger=log) == 2
    assert _positive_int("0", default=2, logger=log) == 2

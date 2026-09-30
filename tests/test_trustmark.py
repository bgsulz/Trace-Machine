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


@needs_models
def test_invisible_analyzer_reports_trustmark(app):
    from veracity.analyzers.invisible import run_invisible_watermarks
    from veracity.analyzers.context import AnalysisContext

    app.config["INVISIBLE_WATERMARK_DECODERS"] = {"adobe_trustmark"}
    app.config["TRUSTMARK_MODEL_DIR"] = str(_model_dir())
    try:
        with app.app_context():
            image_bytes = (FIXTURES / "trustmark_Q.jpg").read_bytes()
            context = AnalysisContext.__new__(AnalysisContext)
            context.image_bytes = image_bytes
            result = run_invisible_watermarks(context)
    finally:
        app.config["INVISIBLE_WATERMARK_DECODERS"] = set()
        app.config.pop("TRUSTMARK_MODEL_DIR", None)

    assert result["status"] == "FOUND"
    finding = result["data"]["findings"][0]
    assert finding["label"] == "Adobe TrustMark"
    assert "Durable Content Credentials" in finding["note"]

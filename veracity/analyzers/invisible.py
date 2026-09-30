from __future__ import annotations

import importlib.util
import io
import logging
import os
import threading
from pathlib import Path
from typing import Any, cast

from flask import current_app
from PIL import Image

from .context import AnalysisContext

logger = logging.getLogger(__name__)


class DecoderUnavailable(Exception):
    pass


_BITS_48: dict[str, int] = {
    "Stable Diffusion XL": 0b101100111110110010010000011110111011000110011110,
    "FLUX.2 (Black Forest Labs)": 0b001010101111111010000111100111001111010100101110,
}
_SD1_STRING = b"StableDiffusionV1"
_MATCH_48 = 44
_MATCH_SD1_FRAC = 0.92

_trustmark_decoders: dict[tuple[str, bool, int], Any] = {}
_trustmark_lock = threading.Lock()


def run_invisible_watermarks(context: AnalysisContext) -> dict[str, object]:
    findings: list[dict[str, object]] = []
    attempted: list[str] = []
    unavailable: list[str] = []
    disabled: list[str] = []
    errors: list[str] = []
    enabled_decoders = _enabled_decoders()

    _run_decoder(
        "open_dwt_dct",
        "Stable Diffusion / SDXL / FLUX DWT-DCT",
        lambda: _detect_open_dwt_dct_watermark(context.image_bytes),
        enabled_decoders,
        attempted,
        unavailable,
        disabled,
        errors,
        findings,
    )
    _run_decoder(
        "adobe_trustmark",
        "Adobe TrustMark",
        lambda: _detect_trustmark(context.image_bytes),
        enabled_decoders,
        attempted,
        unavailable,
        disabled,
        errors,
        findings,
    )

    data = {
        "findings": findings,
        "attempted_decoders": attempted,
        "unavailable_decoders": unavailable,
        "disabled_decoders": disabled,
        "errors": errors,
        "caveat": (
            "Local invisible-watermark detection is positive-only: a hit is "
            "meaningful, but a miss does not prove the image is clean."
        ),
        "has_distant_matches": False,
    }

    if findings:
        names = ", ".join(str(finding["label"]) for finding in findings)
        return {
            "status": "FOUND",
            "summary": f"Local invisible watermark detected: {names}.",
            "data": data,
        }
    if attempted:
        return {
            "status": "NOT FOUND",
            "summary": (
                "No supported invisible watermark decoded. This is inconclusive; "
                "unsupported or stripped watermarks may still be present."
            ),
            "data": data,
        }
    return {
        "status": "NOT AVAILABLE",
        "summary": _not_available_summary(enabled_decoders, unavailable, disabled),
        "data": data,
    }


def _run_decoder(
    key: str,
    label: str,
    detect,
    enabled_decoders: set[str],
    attempted: list[str],
    unavailable: list[str],
    disabled: list[str],
    errors: list[str],
    findings: list[dict[str, object]],
) -> None:
    if key not in enabled_decoders:
        disabled.append(key)
        return

    try:
        result = detect()
    except DecoderUnavailable:
        unavailable.append(key)
        return
    except Exception as exc:
        logger.info("%s invisible-watermark decoder failed: %s", key, exc)
        unavailable.append(key)
        errors.append(f"{label}: {exc}")
        return

    attempted.append(key)
    if result is None:
        return
    finding = dict(result)
    finding.setdefault("key", key)
    finding.setdefault("label", label)
    finding.setdefault("confidence", "high")
    findings.append(finding)


def _enabled_decoders() -> set[str]:
    raw = current_app.config.get("INVISIBLE_WATERMARK_DECODERS", set())
    if isinstance(raw, str):
        return {
            item.strip().lower()
            for item in raw.replace(";", ",").split(",")
            if item.strip()
        }
    return {str(item).strip().lower() for item in raw or [] if str(item).strip()}


def _not_available_summary(
    enabled_decoders: set[str],
    unavailable: list[str],
    disabled: list[str],
) -> str:
    if not enabled_decoders or len(disabled) >= 2:
        return (
            "No local invisible-watermark decoders are enabled on this server."
        )
    if unavailable:
        return "Enabled local invisible-watermark decoders are not installed or failed to load."
    return "No local invisible-watermark decoders are available."


def _module_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def _detect_open_dwt_dct_watermark(image_bytes: bytes) -> dict[str, object] | None:
    if not _module_available("imwatermark"):
        raise DecoderUnavailable
    if not _module_available("cv2") or not _module_available("numpy"):
        raise DecoderUnavailable

    import cv2
    import numpy as np
    from imwatermark import WatermarkDecoder

    image = cv2.imdecode(np.frombuffer(image_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        return None

    try:
        bits = WatermarkDecoder("bits", 48).decode(image, "dwtDct")
        value = 0
        for bit in bits:
            value = (value << 1) | (1 if bit else 0)
        for scheme, ref in _BITS_48.items():
            match = _bits_match(value, ref)
            if match >= _MATCH_48:
                return {
                    "label": "Open DWT-DCT watermark",
                    "scheme": scheme,
                    "match": f"{match}/48 bits",
                    "category": "open_dwt_dct",
                    "note": "Positive-only; most re-encoded or resized copies will not decode.",
                }
    except Exception as exc:
        logger.debug("48-bit invisible watermark decode failed: %s", exc)

    try:
        raw = cast("bytes", WatermarkDecoder("bytes", 8 * len(_SD1_STRING)).decode(image, "dwtDct"))
        match_frac = _bytes_match_frac(raw, _SD1_STRING)
        if match_frac >= _MATCH_SD1_FRAC:
            return {
                "label": "Open DWT-DCT watermark",
                "scheme": "Stable Diffusion 1.x / 2.x",
                "match": f"{match_frac:.0%} bit match",
                "category": "open_dwt_dct",
                "note": "Positive-only; most re-encoded or resized copies will not decode.",
            }
    except Exception as exc:
        logger.debug("string invisible watermark decode failed: %s", exc)

    return None


def _detect_trustmark(image_bytes: bytes) -> dict[str, object] | None:
    if not _module_available("onnxruntime"):
        raise DecoderUnavailable
    from ..watermarks.trustmark import SCHEMA_NAMES, ModelUnavailable

    try:
        decoder = _get_trustmark_decoder()
        with Image.open(io.BytesIO(image_bytes)) as img:
            hit = decoder.decode(img)
    except ModelUnavailable as exc:
        logger.warning("TrustMark decoder unavailable: %s", exc)
        raise DecoderUnavailable from exc
    if hit is None:
        return None
    # Fixed width, so identifiers with leading zeros still match a database.
    payload_hex = f"{int(hit.payload, 2):0{(len(hit.payload) + 3) // 4}x}" if hit.payload else ""
    return {
        "label": "Adobe TrustMark",
        "scheme": f"variant {hit.variant}, {SCHEMA_NAMES.get(hit.schema, hit.schema)}",
        "match": f"confirmed on {hit.confirmations} of 3 re-checks",
        "payload": payload_hex,
        "category": "trustmark",
        "note": (
            "TrustMark is the watermark behind Adobe's Durable Content Credentials. "
            "It means this image was registered for Content Credentials, so a signed "
            "manifest may exist even if it isn't attached to this file. It marks "
            "provenance, not AI generation by itself."
        ),
    }


def _get_trustmark_decoder():
    from ..watermarks.trustmark import TrustMarkDecoder

    config = current_app.config
    model_dir = config.get("TRUSTMARK_MODEL_DIR") or os.path.join(
        current_app.instance_path, "models", "trustmark"
    )
    download = bool(config.get("TRUSTMARK_AUTO_DOWNLOAD", True))
    threads = int(config.get("TRUSTMARK_THREADS", 2))
    key = (str(model_dir), download, threads)
    decoder = _trustmark_decoders.get(key)
    if decoder is None:
        with _trustmark_lock:
            decoder = _trustmark_decoders.get(key)
            if decoder is None:
                decoder = TrustMarkDecoder(Path(model_dir), download=download, threads=threads)
                _trustmark_decoders[key] = decoder
    return decoder


def _bits_match(value: int, ref: int, width: int = 48) -> int:
    return width - bin(value ^ ref).count("1")


def _bytes_match_frac(a: bytes, b: bytes) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    diff = sum(bin(x ^ y).count("1") for x, y in zip(a, b, strict=True))
    return 1.0 - diff / (8 * len(b))

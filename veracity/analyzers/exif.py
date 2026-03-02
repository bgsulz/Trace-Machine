from __future__ import annotations

import json
import logging
from io import BytesIO
from typing import Any

import exifread
from PIL import Image, UnidentifiedImageError

from .context import AnalysisContext

logger = logging.getLogger(__name__)

_AUTOMATIC_KEYS = {"parameters"}
_COMFY_PROMPT_KEYS = {"prompt"}
_COMFY_WORKFLOW_KEYS = {"workflow", "workflow_json"}
_PREVIEW_LIMIT = 200
_BASIC_METADATA_KEYS = {"FileType", "ImageSize", "ColorMode", "BitDepth"}
_EXIF_TAG_ALIASES = {
    40962: "PixelXDimension",
    40963: "PixelYDimension",
}


def run_exif_metadata(context: AnalysisContext) -> dict[str, object]:
    """Scan the image for textual metadata from popular AI tools."""

    try:
        # We need both the Pillow object (for PNG chunks) and raw bytes (for ExifRead)
        with Image.open(BytesIO(context.image_bytes)) as img:
            textual_chunks = _collect_text_chunks(img, context.image_bytes)
    except (UnidentifiedImageError, OSError) as exc:
        logger.info("Unable to parse metadata: %s", exc)
        return {
            "status": "ERROR",
            "summary": "Could not inspect image metadata.",
            "data": {},
        }

    findings: list[dict[str, Any]] = []

    for key, value in textual_chunks.items():
        normalized_key = key.lower()
        if not value:
            continue

        # Check for Automatic1111 / Stable Diffusion
        if normalized_key in _AUTOMATIC_KEYS:
            findings.append(_build_automatic1111_finding(key, value))
            continue

        # Check for ComfyUI
        if normalized_key in _COMFY_PROMPT_KEYS:
            findings.append(_build_comfyui_finding(key, value, kind="prompt"))
            continue
        if normalized_key in _COMFY_WORKFLOW_KEYS:
            findings.append(_build_comfyui_finding(key, value, kind="workflow"))
            continue

    # Sort keys for the "Raw Metadata" display
    sorted_chunks = dict(sorted(textual_chunks.items(), key=lambda kv: kv[0].lower()))

    if not findings:
        return {
            "status": "NOT FOUND",
            "summary": "No AI EXIF metadata detected.",
            "data": {
                "findings": [],
                "chunks": sorted_chunks,
                "has_distant_matches": False,
            },
        }

    summary = (
        f"Found {len(findings)} AI metadata entr{'y' if len(findings) == 1 else 'ies'}."
    )
    return {
        "status": "FOUND",
        "summary": summary,
        "data": {
            "findings": findings,
            "chunks": sorted_chunks,
            "has_distant_matches": False,
        },
    }


def _collect_text_chunks(
    img: Image.Image, image_bytes: bytes | None = None
) -> dict[str, str]:
    chunks: dict[str, str] = {}

    fmt = getattr(img, "format", None)
    chunks["FileType"] = (fmt or "Unknown").upper()
    width = getattr(img, "width", None)
    height = getattr(img, "height", None)
    if width is not None and height is not None:
        chunks["ImageSize"] = f"{width}x{height}"
    color_mode = getattr(img, "mode", None)
    chunks["ColorMode"] = color_mode or "Unknown"  # e.g. RGB, RGBA, L

    bit_depth_map = {"1": 1, "L": 8, "P": 8, "RGB": 8, "RGBA": 8, "CMYK": 8, "I;16": 16}
    if color_mode in bit_depth_map:
        chunks["BitDepth"] = str(bit_depth_map[color_mode])

    info = getattr(img, "info", {}) or {}
    for key, value in info.items():
        if key in ("exif", "icc_profile"):  # Skip binary blobs
            continue
        string_value = _stringify_metadata_value(value)
        if string_value:
            chunks[str(key)] = string_value

    pillow_exif = None
    getexif_fn = getattr(img, "getexif", None)
    if callable(getexif_fn):
        try:
            pillow_exif = getexif_fn()
        except Exception:
            pillow_exif = None

    if pillow_exif:
        for tag, value in pillow_exif.items():
            clean_key = _normalize_exif_key(tag)
            val_str = _stringify_metadata_value(value)
            if val_str:
                chunks[clean_key] = val_str

    exif_bytes = None
    if image_bytes is None:
        image_bytes = b""

    # If Pillow found an EXIF blob (common in PNGs), use that.
    if "exif" in info and isinstance(info["exif"], bytes):
        exif_bytes = info["exif"]
    # Otherwise, if it's a JPEG/TIFF, use the whole file.
    elif fmt in ("JPEG", "TIFF", "WEBP"):
        exif_bytes = image_bytes

    if exif_bytes:
        try:
            tags = exifread.process_file(BytesIO(exif_bytes), details=False)
            for tag, value in tags.items():
                if tag in (
                    "JPEGThumbnail",
                    "TIFFThumbnail",
                    "Filename",
                    "EXIF MakerNote",
                ):
                    continue

                # Clean up key names
                clean_key = _normalize_exif_key(tag)

                val_str = str(value)
                if val_str and val_str.strip():
                    chunks[clean_key] = val_str
        except Exception as exc:
            logger.debug("ExifRead extraction failed: %s", exc)

    return chunks


def _stringify_metadata_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value.decode("latin-1", errors="replace")
    if isinstance(value, (list, tuple)):
        return ", ".join(_stringify_metadata_value(item) or "" for item in value).strip(
            ", "
        )
    if isinstance(value, dict):
        try:
            return json.dumps(value, ensure_ascii=False)
        except TypeError:
            return str(value)
    return str(value)


def _normalize_exif_key(tag: Any) -> str:
    if tag in _EXIF_TAG_ALIASES:
        return _EXIF_TAG_ALIASES[tag]
    clean_key = str(tag)
    if clean_key.startswith("EXIF "):
        return clean_key[5:]
    if clean_key.startswith("Image "):
        return clean_key[6:]
    return clean_key


def _build_automatic1111_finding(key: str, raw_value: str) -> dict[str, Any]:
    prompt, settings = _split_automatic_prompt(raw_value)
    metadata: dict[str, Any] = {}
    if prompt:
        metadata["prompt"] = prompt
    metadata.update(_parse_key_values(settings))

    return {
        "tool": "Automatic1111",
        "key": key,
        "preview": _make_preview(raw_value),
        "full_text": raw_value,
        "metadata": metadata,
    }


def _split_automatic_prompt(value: str) -> tuple[str, str]:
    marker = "Steps:"
    if marker in value:
        before, after = value.split(marker, 1)
        prompt = before.strip().rstrip(",")
        settings = f"{marker}{after}".strip()
        return prompt, settings
    return "", value.strip()


def _parse_key_values(blob: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    parts = [segment.strip() for segment in blob.split(",") if segment.strip()]
    for part in parts:
        if ":" not in part:
            continue
        key, value = part.split(":", 1)
        key = key.strip().lower().replace(" ", "_")
        result[key] = value.strip()
    return result


def _build_comfyui_finding(key: str, raw_value: str, *, kind: str) -> dict[str, Any]:
    metadata: dict[str, Any] = {"kind": kind}
    parsed_json: Any | None = None
    trimmed = raw_value.strip()
    if trimmed.startswith("{"):
        try:
            parsed_json = json.loads(trimmed)
        except json.JSONDecodeError:
            parsed_json = None
    if parsed_json is not None:
        metadata["parsed_json"] = parsed_json

    return {
        "tool": "ComfyUI",
        "key": key,
        "preview": _make_preview(raw_value),
        "full_text": raw_value,
        "metadata": metadata,
    }


def _make_preview(value: str, limit: int = _PREVIEW_LIMIT) -> str:
    normalized = " ".join(value.strip().split())
    if len(normalized) <= limit:
        return normalized
    return normalized[:limit].rstrip() + "…"

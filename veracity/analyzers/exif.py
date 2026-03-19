from __future__ import annotations

import json
import logging
import re
import xml.etree.ElementTree as ET
from io import BytesIO
from typing import Any

import exifread
from PIL import Image, UnidentifiedImageError
from PIL.ExifTags import GPSTAGS, IFD, TAGS

from .context import AnalysisContext

logger = logging.getLogger(__name__)

_AUTOMATIC_KEYS = {"parameters"}
_COMFY_PROMPT_KEYS = {"prompt"}
_COMFY_WORKFLOW_KEYS = {"workflow", "workflow_json"}
_PREVIEW_LIMIT = 200
_BASIC_METADATA_KEYS = {"FileType", "ImageSize", "ColorMode", "BitDepth"}

# Keys shown in the always-visible friendly summary (order matters)
_FRIENDLY_KEYS = [
    ("FileType", "File type"),
    ("ImageSize", "Dimensions"),
    ("ColorMode", "Color mode"),
    ("Software", "Software"),
    ("Make", "Camera make"),
    ("Model", "Camera model"),
    ("LensModel", "Lens"),
    ("DateTimeOriginal", "Date taken"),
    ("XMP:xmp:CreatorTool", "Creator tool"),
    ("XMP:dc:creator", "Creator"),
    ("XMP:photoshop:Credit", "Credit"),
]

# Category grouping for the raw metadata table
_CATEGORY_PREFIXES: list[tuple[str, str]] = [
    ("GPS:", "GPS"),
    ("XMP:", "XMP"),
]
_CAMERA_KEYS = {
    "Make", "Model", "LensModel", "LensMake", "FocalLength", "FNumber",
    "ExposureTime", "ISOSpeedRatings", "ISO", "ShutterSpeedValue",
    "ApertureValue", "BrightnessValue", "ExposureBiasValue", "MaxApertureValue",
    "MeteringMode", "Flash", "FocalLengthIn35mmFilm", "WhiteBalance",
    "ExposureProgram", "ExposureMode", "SceneCaptureType", "DigitalZoomRatio",
    "DateTimeOriginal", "DateTimeDigitized", "OffsetTime", "OffsetTimeOriginal",
    "SubSecTimeOriginal", "SubSecTimeDigitized", "Software",
}
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

    # Check XMP fields for known AI-tool indicators
    for xmp_key, pattern in _AI_XMP_INDICATORS.items():
        xmp_value = textual_chunks.get(xmp_key, "")
        if xmp_value and pattern.search(xmp_value):
            findings.append(_build_xmp_ai_finding(xmp_key, xmp_value))

    # Build friendly summary (always-visible key facts)
    friendly_summary = _build_friendly_summary(textual_chunks)

    # Group chunks by category for the raw metadata display
    grouped_chunks = _categorize_chunks(textual_chunks)

    # Flat sorted chunks kept for backwards compat
    sorted_chunks = dict(sorted(textual_chunks.items(), key=lambda kv: kv[0].lower()))

    data = {
        "findings": findings,
        "chunks": sorted_chunks,
        "grouped_chunks": grouped_chunks,
        "friendly_summary": friendly_summary,
        "has_distant_matches": False,
    }

    if not findings:
        return {
            "status": "NOT FOUND",
            "summary": "No AI EXIF metadata detected.",
            "data": data,
        }

    summary = (
        f"Found {len(findings)} AI metadata entr{'y' if len(findings) == 1 else 'ies'}."
    )
    return {
        "status": "FOUND",
        "summary": summary,
        "data": data,
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
        # IFD0 (top-level) tags
        for tag, value in pillow_exif.items():
            clean_key = _normalize_exif_key(tag)
            val_str = _stringify_metadata_value(value)
            if val_str:
                chunks[clean_key] = val_str

        # EXIF sub-IFD (DateTimeOriginal, LensModel, ISO, etc.)
        _extract_ifd(pillow_exif, IFD.Exif, TAGS, chunks)
        # GPS sub-IFD
        _extract_ifd(pillow_exif, IFD.GPSInfo, GPSTAGS, chunks, prefix="GPS:")
        # Interop sub-IFD
        _extract_ifd(pillow_exif, IFD.Interop, TAGS, chunks)

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
            tags = exifread.process_file(BytesIO(exif_bytes), details=True)
            for tag, value in tags.items():
                if tag in (
                    "JPEGThumbnail",
                    "TIFFThumbnail",
                    "Filename",
                    "EXIF MakerNote",
                ):
                    continue
                # Skip noisy MakerNote sub-tags
                if "MakerNote" in tag:
                    continue

                clean_key = _normalize_exif_key(tag)

                val_str = str(value)
                if val_str and val_str.strip():
                    chunks[clean_key] = val_str
        except Exception as exc:
            logger.debug("ExifRead extraction failed: %s", exc)

    # XMP extraction
    xmp_tags = _extract_xmp(img, image_bytes)
    chunks.update(xmp_tags)

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


def _build_friendly_summary(chunks: dict[str, str]) -> list[dict[str, str]]:
    """Pick out the most useful fields for a non-technical audience."""
    summary: list[dict[str, str]] = []
    for raw_key, label in _FRIENDLY_KEYS:
        value = chunks.get(raw_key, "")
        if value:
            summary.append({"label": label, "value": value})
    return summary


def _categorize_chunks(
    chunks: dict[str, str],
) -> list[dict[str, Any]]:
    """Split flat chunks dict into ordered category groups."""
    buckets: dict[str, dict[str, str]] = {
        "Basic": {},
        "Camera": {},
        "GPS": {},
        "XMP": {},
        "Other": {},
    }

    for key, value in chunks.items():
        placed = False
        # Check prefix-based categories first
        for prefix, cat in _CATEGORY_PREFIXES:
            if key.startswith(prefix):
                buckets[cat][key] = value
                placed = True
                break
        if placed:
            continue
        if key in _BASIC_METADATA_KEYS:
            buckets["Basic"][key] = value
        elif key in _CAMERA_KEYS:
            buckets["Camera"][key] = value
        else:
            buckets["Other"][key] = value

    # Return only non-empty groups, sorted alphabetically within each
    result: list[dict[str, Any]] = []
    for cat_name in ("Basic", "Camera", "GPS", "XMP", "Other"):
        cat_items = buckets[cat_name]
        if cat_items:
            sorted_items = dict(sorted(cat_items.items(), key=lambda kv: kv[0].lower()))
            result.append({"name": cat_name, "entries": sorted_items})
    return result


def _extract_ifd(
    exif_data: Any,
    ifd_key: int,
    tag_lookup: dict[int, str],
    chunks: dict[str, str],
    prefix: str = "",
) -> None:
    """Extract tags from a Pillow EXIF sub-IFD into *chunks*."""
    try:
        ifd = exif_data.get_ifd(ifd_key)
    except Exception:
        return
    if not ifd:
        return
    for tag, value in ifd.items():
        tag_name = tag_lookup.get(tag) or _EXIF_TAG_ALIASES.get(tag) or str(tag)
        val_str = _stringify_metadata_value(value)
        if val_str:
            chunks[f"{prefix}{tag_name}"] = val_str


# ---------------------------------------------------------------------------
# XMP extraction
# ---------------------------------------------------------------------------

_XMP_START = b"<x:xmpmeta"
_XMP_END = b"</x:xmpmeta>"

# Namespace prefixes we care about → human-readable prefix
_XMP_NS_MAP: dict[str, str] = {
    "http://purl.org/dc/elements/1.1/": "dc",
    "http://ns.adobe.com/xap/1.0/": "xmp",
    "http://ns.adobe.com/photoshop/1.0/": "photoshop",
    "http://ns.adobe.com/tiff/1.0/": "tiff",
    "http://ns.adobe.com/exif/1.0/": "exif",
    "http://ns.adobe.com/xap/1.0/mm/": "xmpMM",
    "http://ns.adobe.com/xap/1.0/rights/": "xmpRights",
    "http://ns.adobe.com/camera-raw-settings/1.0/": "crs",
    "http://ns.adobe.com/adobeillustrator/10.0/": "ai",
}

# rdf namespace
_RDF_NS = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"


def _extract_xmp(img: Image.Image, image_bytes: bytes) -> dict[str, str]:
    """Pull XMP key-value pairs from the image, prefixed with ``XMP:``."""
    xmp_blob = _find_xmp_blob(img, image_bytes)
    if not xmp_blob:
        return {}

    try:
        root = ET.fromstring(xmp_blob)
    except ET.ParseError:
        logger.debug("XMP XML could not be parsed")
        return {}

    results: dict[str, str] = {}

    # Walk every element in the XMP tree
    for elem in root.iter():
        ns, local = _split_ns(elem.tag)
        prefix = _XMP_NS_MAP.get(ns, "")
        qualified = f"{prefix}:{local}" if prefix else local

        # Grab simple text content (leaf elements)
        if elem.text and elem.text.strip():
            text = elem.text.strip()
            results[f"XMP:{qualified}"] = text

        # Grab rdf:Description attributes (common in XMP)
        for attr, val in elem.attrib.items():
            attr_ns, attr_local = _split_ns(attr)
            if attr_ns == _RDF_NS:
                continue  # skip rdf:about, rdf:parseType, etc.
            attr_prefix = _XMP_NS_MAP.get(attr_ns, "")
            attr_qualified = f"{attr_prefix}:{attr_local}" if attr_prefix else attr_local
            if val and val.strip():
                results[f"XMP:{attr_qualified}"] = val.strip()

    return results


def _find_xmp_blob(img: Image.Image, image_bytes: bytes) -> bytes | None:
    """Locate the XMP packet from Pillow info or raw bytes."""
    info = getattr(img, "info", {}) or {}

    # Pillow sometimes stores it under "xml" (PNG) or "xmp" (WEBP)
    for key in ("xml", "xmp"):
        raw = info.get(key)
        if raw:
            if isinstance(raw, list):
                raw = raw[0] if raw else None
            if isinstance(raw, str):
                return raw.encode("utf-8")
            if isinstance(raw, bytes):
                return raw

    # Fall back: scan the raw bytes for the XMP packet
    if image_bytes:
        start = image_bytes.find(_XMP_START)
        if start != -1:
            end = image_bytes.find(_XMP_END, start)
            if end != -1:
                return image_bytes[start : end + len(_XMP_END)]

    return None


def _split_ns(tag: str) -> tuple[str, str]:
    """Split an ElementTree ``{namespace}local`` tag into ``(ns, local)``."""
    if tag.startswith("{"):
        ns, local = tag[1:].split("}", 1)
        return ns, local
    return "", tag


# ---------------------------------------------------------------------------
# AI-tool XMP detection
# ---------------------------------------------------------------------------

# Known AI tool identifiers that may appear in XMP fields
_AI_XMP_INDICATORS: dict[str, re.Pattern[str]] = {
    "XMP:xmp:CreatorTool": re.compile(
        r"(firefly|midjourney|dall[·\-\s]?e|stable.?diffusion|"
        r"comfyui|invoke.?ai|novelai|leonardo\.ai|ideogram|flux)",
        re.IGNORECASE,
    ),
    "XMP:photoshop:Credit": re.compile(
        r"(ai[- ]generated|made.?with.?ai)", re.IGNORECASE
    ),
    "XMP:dc:description": re.compile(
        r"(firefly|midjourney|dall[·\-\s]?e|stable.?diffusion)", re.IGNORECASE
    ),
}


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


def _build_xmp_ai_finding(key: str, value: str) -> dict[str, Any]:
    return {
        "tool": "XMP AI Indicator",
        "key": key,
        "preview": _make_preview(value),
        "full_text": value,
        "metadata": {"field": key, "value": value},
    }


def _make_preview(value: str, limit: int = _PREVIEW_LIMIT) -> str:
    normalized = " ".join(value.strip().split())
    if len(normalized) <= limit:
        return normalized
    return normalized[:limit].rstrip() + "…"

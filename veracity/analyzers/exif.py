from __future__ import annotations

import json
import logging
from io import BytesIO
from typing import Any

from PIL import Image, UnidentifiedImageError

from .context import AnalysisContext

logger = logging.getLogger(__name__)


_AUTOMATIC_KEYS = {"parameters"}
_COMFY_PROMPT_KEYS = {"prompt"}
_COMFY_WORKFLOW_KEYS = {"workflow", "workflow_json"}
_PREVIEW_LIMIT = 200


def run_exif_metadata(context: AnalysisContext) -> dict[str, object]:
    """Scan the image for textual metadata from popular AI tools."""

    try:
        with Image.open(BytesIO(context.image_bytes)) as img:
            textual_chunks = _collect_text_chunks(img)
    except (UnidentifiedImageError, OSError) as exc:
        logger.info("Unable to parse EXIF metadata: %s", exc)
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

        if normalized_key in _AUTOMATIC_KEYS:
            findings.append(_build_automatic1111_finding(key, value))
            continue

        if normalized_key in _COMFY_PROMPT_KEYS:
            findings.append(_build_comfyui_finding(key, value, kind="prompt"))
            continue

        if normalized_key in _COMFY_WORKFLOW_KEYS:
            findings.append(_build_comfyui_finding(key, value, kind="workflow"))
            continue

    if not findings:
        return {
            "status": "NOT FOUND",
            "summary": "No AI EXIF metadata detected.",
            "data": {"findings": []},
        }

    summary = (
        f"Found {len(findings)} AI metadata entr{'y' if len(findings) == 1 else 'ies'}."
    )
    return {
        "status": "FOUND",
        "summary": summary,
        "data": {"findings": findings},
    }


def _collect_text_chunks(img: Image.Image) -> dict[str, str]:
    chunks: dict[str, str] = {}
    info = getattr(img, "info", {}) or {}
    for key, value in info.items():
        if isinstance(value, bytes):
            try:
                decoded = value.decode("utf-8", errors="replace")
            except Exception:  # pragma: no cover - defensive
                decoded = value.decode("latin-1", errors="replace")
            chunks[key] = decoded
        elif isinstance(value, str):
            chunks[key] = value
    return chunks


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


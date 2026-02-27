from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from flask import current_app

from ..analysis_cache import load_analysis_payload, load_cached_analyzer_row
from ..analyzers.manager import ANALYZERS
from ..registry import prepare_analysis_context
from .trace_service import build_direct_and_distant_traces
from ..tools import generate_external_tools

REPORT_VERSION = "1.1"


def build_report_payload(analysis_id: str) -> dict[str, Any]:
    payload = load_analysis_payload(analysis_id)
    if payload is None:
        raise KeyError(f"Unknown analysis id: {analysis_id}")

    image_bytes, metadata = payload
    source = metadata.get("source")
    mime_type = metadata.get("mime_type")
    created_at = _iso_utc(metadata.get("created_at"))
    public_url = metadata.get("public_url")
    analyzer_rows = _build_analyzer_rows(analysis_id)

    context = prepare_analysis_context(image_bytes)
    trace_summary = build_direct_and_distant_traces(
        context,
        analyzer_rows=analyzer_rows,
    )

    report: dict[str, Any] = {
        "report_version": REPORT_VERSION,
        "generated_at": _iso_utc(datetime.now(timezone.utc).timestamp()),
        "app_version": current_app.config.get("APP_VERSION"),
        "analysis": {
            "id": analysis_id,
            "created_at": created_at,
            "source": source,
            "mime": mime_type,
            "dimensions": {
                "width": metadata.get("image_width"),
                "height": metadata.get("image_height"),
            },
            "crop_box": _normalize_crop_box(metadata.get("crop_box")),
        },
        "hashes": {
            "phash": metadata.get("phash"),
            "whash": metadata.get("whash"),
            "registry_id": metadata.get("registry_id"),
        },
        "analyzers": analyzer_rows,
        "direct_traces": trace_summary["direct_items"],
        "distant_matches": trace_summary["distant_matches"],
        "tools": generate_external_tools(public_url, analysis_id=analysis_id),
    }
    return report


def _build_analyzer_rows(analysis_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for spec in ANALYZERS:
        raw_row = load_cached_analyzer_row(analysis_id, spec.slug)
        if raw_row is None:
            rows.append(
                {
                    "name": spec.name,
                    "slug": spec.slug,
                    "status": "NOT_RUN",
                    "summary": "Analyzer row is not available yet.",
                    "data": {},
                    "template": spec.template,
                }
            )
            continue
        rows.append(_normalize_analyzer_row(raw_row, spec.name, spec.slug, spec.template))
    return rows


def _normalize_analyzer_row(
    row: dict[str, Any],
    fallback_name: str,
    fallback_slug: str,
    fallback_template: str,
) -> dict[str, Any]:
    data = row.get("data")
    if not isinstance(data, dict):
        data = {}
    return {
        "name": row.get("name", fallback_name),
        "slug": row.get("slug", fallback_slug),
        "status": str(row.get("status", "UNKNOWN")),
        "summary": str(row.get("summary", "")),
        "data": data,
        "template": row.get("template", fallback_template),
    }


def _normalize_crop_box(
    crop_box: tuple[float, float, float, float] | list[float] | None,
) -> dict[str, float] | None:
    if not crop_box or len(crop_box) != 4:
        return None
    left, top, width, height = crop_box
    try:
        return {
            "left": float(left),
            "top": float(top),
            "width": float(width),
            "height": float(height),
        }
    except (TypeError, ValueError):
        return None


def _iso_utc(value: Any) -> str | None:
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )

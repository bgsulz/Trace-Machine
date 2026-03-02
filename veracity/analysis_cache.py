import json
import time
import uuid
from pathlib import Path
from typing import Any

from flask import current_app

ANALYSIS_DIRNAME = "analysis_cache"
ANALYSIS_BYTES_SUFFIX = ".bin"
ANALYSIS_META_SUFFIX = ".json"


def analysis_dir() -> Path:
    base = Path(current_app.instance_path) / ANALYSIS_DIRNAME
    base.mkdir(parents=True, exist_ok=True)
    return base


def analysis_bytes_path(analysis_id: str) -> Path:
    return analysis_dir() / f"{analysis_id}{ANALYSIS_BYTES_SUFFIX}"


def analysis_meta_path(analysis_id: str) -> Path:
    return analysis_dir() / f"{analysis_id}{ANALYSIS_META_SUFFIX}"


def analysis_row_path(analysis_id: str, slug: str) -> Path:
    return analysis_dir() / f"{analysis_id}-{slug}.row.json"


def store_analysis_payload(
    analysis_id: str | None,
    image_bytes: bytes,
    metadata: dict[str, Any],
) -> str:
    token = analysis_id or uuid.uuid4().hex
    data_path = analysis_bytes_path(token)
    meta_path = analysis_meta_path(token)
    data_path.write_bytes(image_bytes)
    metadata = {**metadata, "created_at": time.time()}
    meta_path.write_text(json.dumps(metadata), encoding="utf-8")
    return token


def load_analysis_metadata(analysis_id: str) -> dict[str, Any] | None:
    """Load only the JSON metadata for an analysis (no image bytes)."""
    meta_path = analysis_meta_path(analysis_id)
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def update_analysis_metadata(analysis_id: str, updates: dict[str, Any]) -> None:
    """Merge *updates* into existing metadata on disk."""
    meta_path = analysis_meta_path(analysis_id)
    try:
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return
    metadata.update(updates)
    meta_path.write_text(json.dumps(metadata), encoding="utf-8")


def load_analysis_payload(
    analysis_id: str,
) -> tuple[bytes, dict[str, Any]] | None:
    data_path = analysis_bytes_path(analysis_id)
    meta_path = analysis_meta_path(analysis_id)
    try:
        image_bytes = data_path.read_bytes()
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    return image_bytes, metadata


def store_cached_analyzer_row(analysis_id: str, slug: str, row: dict[str, Any]) -> None:
    path = analysis_row_path(analysis_id, slug)
    serialized = json.dumps(row, default=str)
    path.write_text(serialized, encoding="utf-8")


def load_cached_analyzer_row(analysis_id: str, slug: str) -> dict[str, Any] | None:
    path = analysis_row_path(analysis_id, slug)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None

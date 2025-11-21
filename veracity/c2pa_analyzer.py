from __future__ import annotations

import json
import logging
from io import BytesIO

from PIL import Image, UnidentifiedImageError

try:  # pragma: no cover - import guard
    from c2pa import Reader
except ImportError:  # pragma: no cover - handled at runtime
    Reader = None

logger = logging.getLogger(__name__)


_FORMAT_TO_MIME = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
    "GIF": "image/gif",
}


def _detect_mime_type(image_bytes: bytes) -> str:
    try:
        with Image.open(BytesIO(image_bytes)) as img:
            fmt = (img.format or "").upper()
    except (UnidentifiedImageError, OSError):
        return "application/octet-stream"
    return _FORMAT_TO_MIME.get(fmt, "application/octet-stream")


def run_c2pa(image_bytes: bytes) -> tuple[str, str]:
    """Run the C2PA analyzer using the c2pa-python Reader.

    Returns (status, details) suitable for the analysis table.
    """
    if Reader is None:
        return "NOT AVAILABLE", "c2pa-python dependency is not installed."

    mime_type = _detect_mime_type(image_bytes)
    try:
        with Reader(mime_type, BytesIO(image_bytes)) as reader:  # type: ignore[arg-type]
            manifest_store = json.loads(reader.json())
    except Exception as exc:  # pragma: no cover - defensive logging
        logger.exception("C2PA analyzer failed")
        return "ERROR", f"Failed to read C2PA manifest: {exc}"

    manifests = manifest_store.get("manifests") or {}
    active_id = manifest_store.get("active_manifest")
    active_manifest = manifests.get(active_id) if active_id else None

    if not active_manifest:
        return "NOT FOUND", "No C2PA manifest detected."

    signer = (
        active_manifest.get("signature_info", {}).get("issuer")
        or active_manifest.get("claim_generator")
        or "Unknown signer"
    )
    details = f"Signed by {signer}"
    return "FOUND", details

from __future__ import annotations

import json
import logging
from io import BytesIO
from typing import TYPE_CHECKING

from PIL import Image, UnidentifiedImageError

from .. import db
from ..models import ProvenanceFact

if TYPE_CHECKING:  # pragma: no cover - type checking only
    from .manager import AnalysisContext

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


def _run_c2pa_tool(image_bytes: bytes) -> dict[str, object]:
    """Run the low-level C2PA tool on raw bytes.

    Returns a dict with keys: status, summary, data.
    """
    if Reader is None:
        return {
            "status": "NOT AVAILABLE",
            "summary": "c2pa-python dependency is not installed.",
            "data": {},
        }

    mime_type = _detect_mime_type(image_bytes)
    try:
        with Reader(mime_type, BytesIO(image_bytes)) as reader:  # type: ignore[arg-type]
            manifest_store = json.loads(reader.json())
    except Exception as exc:  # pragma: no cover - defensive logging
        message = str(exc)
        if (
            "ManifestNotFound" in type(exc).__name__
            or "ManifestNotFound" in message
            or "no JUMBF data found" in message
        ):
            logger.info("No C2PA manifest found: %s", exc)
            return {
                "status": "NOT FOUND",
                "summary": "No C2PA signature found.",
                "data": {"has_manifest": False},
            }

        logger.exception("C2PA analyzer failed")
        return {
            "status": "ERROR",
            "summary": f"Failed to read C2PA manifest: {exc}",
            "data": {},
        }

    manifests = manifest_store.get("manifests") or {}
    active_id = manifest_store.get("active_manifest")
    active_manifest = manifests.get(active_id) if active_id else None

    if not active_manifest:
        return {
            "status": "NOT FOUND",
            "summary": "No C2PA signature found.",
            "data": {"has_manifest": False},
        }

    signer = (
        active_manifest.get("signature_info", {}).get("issuer")
        or active_manifest.get("claim_generator")
        or "Unknown signer"
    )
    claim_generator = active_manifest.get("claim_generator") or ""
    ingredients = active_manifest.get("ingredients") or []
    provenance_depth = len(ingredients) if isinstance(ingredients, list) else None

    signature_status: str | None = None
    if isinstance(ingredients, list) and ingredients:
        first_ingredient = ingredients[0]
        validation = (first_ingredient.get("validation_results") or {}).get(
            "activeManifest", {}
        )
        if isinstance(validation, dict):
            failures = validation.get("failure") or []
            successes = validation.get("success") or []
            if failures:
                signature_status = "invalid"
            elif successes:
                signature_status = "valid"

    summary = f"Signed by {signer}"
    data: dict[str, object] = {
        "tool": claim_generator,
        "has_manifest": True,
        "provenance_depth": provenance_depth,
    }
    if signature_status is not None:
        data["signature_status"] = signature_status

    return {
        "status": "FOUND",
        "summary": summary,
        "data": data,
    }


def run_c2pa(context: "AnalysisContext") -> dict[str, object]:
    """Run the C2PA analyzer with caching based on the analysis context."""

    # 1. Run the tool on the raw bytes ("new" analysis).
    result = _run_c2pa_tool(context.image_bytes)

    # If the tool is unavailable or errored, just return that directly.
    if result["status"] in {"NOT AVAILABLE", "ERROR"}:
        return result

    # 2. If we found fresh metadata, save it to the DB and return.
    if result["status"] == "FOUND":
        try:
            fact = ProvenanceFact(
                image_id=context.registry_id,
                analyzer="c2pa",
                data=str(result.get("summary", "")),
            )
            db.session.add(fact)
            db.session.commit()
        except Exception:  # pragma: no cover - defensive
            db.session.rollback()
            logger.exception("Failed to persist C2PA provenance fact")
        return result

    # 3. If NOT found, check the neighbors (cached analysis from similar images).
    for neighbor in context.neighbors:
        for fact in getattr(neighbor, "facts", []) or []:
            if fact.analyzer == "c2pa":
                return {
                    "status": "FOUND (MATCH)",
                    "summary": (
                        f"Visual match (hash {neighbor.phash}) had C2PA: {fact.data}"
                    ),
                    "data": {
                        "has_manifest": True,
                        "source": "neighbor-cache",
                        "neighbor_phash": neighbor.phash,
                    },
                }

    # Still nothing: propagate a NOT FOUND status.
    return {
        "status": "NOT FOUND",
        "summary": "No C2PA signature found.",
        "data": {"has_manifest": False},
    }

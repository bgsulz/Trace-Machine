from __future__ import annotations
import json
import logging
from io import BytesIO
from PIL import Image, UnidentifiedImageError
from .. import db
from ..models import ProvenanceFact
from .context import AnalysisContext
from .hash_utils import (
    compute_base_hashes,
    compute_neighbor_distances,
    extract_sources,
)

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
    "AVIF": "image/avif",  # in case Pillow supports it
}

_AVIF_BRANDS = {b"avif", b"avis", b"av01", b"mif1", b"msf1"}


def _detect_mime_type(image_bytes: bytes) -> str:
    try:
        with Image.open(BytesIO(image_bytes)) as img:
            fmt = (img.format or "").upper()
    except (UnidentifiedImageError, OSError):
        pass
    else:
        return _FORMAT_TO_MIME.get(fmt, "application/octet-stream")

    # ISO-BMFF header check for AVIF/HEIF
    if len(image_bytes) >= 12:
        if image_bytes[4:8] == b"ftyp" and image_bytes[8:12] in _AVIF_BRANDS:
            return "image/avif"

    return "application/octet-stream"


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


def run_c2pa(context: AnalysisContext) -> dict[str, object]:
    """Run the C2PA analyzer with caching based on the analysis context."""

    # 1. Run the tool on the raw bytes ("new" analysis).
    result = _run_c2pa_tool(context.image_bytes)

    # If the tool is unavailable or errored, just return that directly.
    if result["status"] in {"NOT AVAILABLE", "ERROR"}:
        return result

    status = str(result.get("status", "UNKNOWN"))

    # 2. If we found fresh metadata, save it to the DB.
    if status == "FOUND":
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

    # 3. Build a list of nearby matches from provenance facts on neighbors.
    matches: list[dict[str, object]] = []
    base_phash, base_whash = compute_base_hashes(context.phash, context.whash)

    for neighbor in context.neighbors:
        phash = getattr(neighbor, "phash", None)
        if not phash:
            continue

        neighbor_whash_val = getattr(neighbor, "whash", None)
        (
            phash_distance,
            whash_distance,
            display_hash,
            display_label,
            display_distance,
        ) = compute_neighbor_distances(
            base_phash, base_whash, phash, neighbor_whash_val
        )

        for fact in getattr(neighbor, "facts", []) or []:
            if fact.analyzer != "c2pa":
                continue

            sources = extract_sources(neighbor)

            matches.append(
                {
                    "phash": phash,
                    "whash": neighbor_whash_val,
                    "hash_display": f"{display_hash} ({display_label})",
                    "distance": display_distance,
                    "distance_phash": phash_distance,
                    "distance_whash": whash_distance,
                    "fact_data": str(fact.data),
                    "sources": sources,
                }
            )

    # If the tool itself found nothing but we have cached matches, adjust status.
    if status == "NOT FOUND" and matches:
        result["status"] = "SIMILAR"
        result["summary"] = (
            f"Found {len(matches)} visually similar images with C2PA metadata."
        )

    # Attach matches for the UI layer; always include the key for consistency.
    data = result.get("data") or {}
    data["matches"] = matches
    result["data"] = data

    return result

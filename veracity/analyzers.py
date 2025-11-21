"""Analyzer execution engine and concrete implementations for Phase 3."""
from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, Future, as_completed
from dataclasses import dataclass
from io import BytesIO
from typing import Callable, Iterable, Sequence

import imagehash
from PIL import Image, UnidentifiedImageError

logger = logging.getLogger(__name__)

try:  # pragma: no cover - import guard
    from c2pa import Reader
except ImportError:  # pragma: no cover - handled at runtime
    Reader = None


@dataclass(frozen=True)
class AnalyzerSpec:
    name: str
    func: Callable[[bytes], tuple[str, str]]


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


def _digital_signature_c2pa(image_bytes: bytes) -> tuple[str, str]:
    """Run the real C2PA analyzer using the c2pa-python Reader."""
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


def _synthid_stub(image_bytes: bytes) -> tuple[str, str]:
    """Placeholder SynthID analyzer (real API pending)."""
    checksum = sum(image_bytes[:32]) % 5 if image_bytes else 0
    detected = checksum == 0
    status = "DETECTED" if detected else "NOT DETECTED"
    details = "SynthID integration pending (stub output)."
    if detected:
        details += f" Checksum bucket={checksum}."
    return status, details


def _human_consensus_phash(image_bytes: bytes) -> tuple[str, str]:
    """Compute a perceptual hash via ImageHash (DB wiring lands in Phase 4)."""
    try:
        with Image.open(BytesIO(image_bytes)) as img:
            hash_value = imagehash.phash(img)
    except (UnidentifiedImageError, OSError) as exc:  # pragma: no cover - defensive
        logger.exception("Human consensus analyzer failed")
        return "ERROR", f"Failed to compute perceptual hash: {exc}"

    return "HASHED", f"phash={hash_value} (consensus DB pending)"


ANALYZERS: Sequence[AnalyzerSpec] = (
    AnalyzerSpec(name="Digital Signature (C2PA)", func=_digital_signature_c2pa),
    AnalyzerSpec(name="Google SynthID", func=_synthid_stub),
    AnalyzerSpec(name="Human Consensus", func=_human_consensus_phash),
)


def run_all_analyzers(
    image_bytes: bytes,
    analyzers: Iterable[AnalyzerSpec] | None = None,
) -> list[dict[str, str]]:
    """Execute all analyzers in parallel and normalize their outputs."""
    specs: Sequence[AnalyzerSpec]
    if analyzers is None:
        specs = tuple(ANALYZERS)
    else:
        specs = tuple(analyzers)

    if not specs:
        return []

    results_map: dict[str, dict[str, str]] = {}
    future_spec: dict[Future[tuple[str, str]], AnalyzerSpec] = {}
    start_times: dict[Future[tuple[str, str]], float] = {}

    max_workers = len(specs)
    logger.debug("Submitting %d analyzers using %d workers", len(specs), max_workers)
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="analyzer") as executor:
        for spec in specs:
            logger.info("Analyzer '%s' starting", spec.name)
            future = executor.submit(spec.func, image_bytes)
            future_spec[future] = spec
            start_times[future] = time.perf_counter()

        for future in as_completed(future_spec):
            spec = future_spec[future]
            elapsed = time.perf_counter() - start_times[future]
            try:
                status, details = future.result()
            except Exception as exc:  # pragma: no cover - defensive logging
                logger.exception("Analyzer '%s' failed", spec.name)
                status, details = "ERROR", str(exc)
            else:
                logger.info(
                    "Analyzer '%s' finished on %s in %.3fs",
                    spec.name,
                    threading.current_thread().name,
                    elapsed,
                )
            results_map[spec.name] = {
                "name": spec.name,
                "status": status,
                "details": details,
            }

    return [results_map[spec.name] for spec in specs]

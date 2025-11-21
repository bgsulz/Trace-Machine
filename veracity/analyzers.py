"""Stub analyzer execution engine for Phase 2."""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, Future, as_completed
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AnalyzerSpec:
    name: str
    func: Callable[[bytes], tuple[str, str]]


def _digital_signature_stub(image_bytes: bytes) -> tuple[str, str]:
    """Fake C2PA analyzer that toggles result based on payload parity."""
    signed = len(image_bytes) % 2 == 0
    if signed:
        return "FOUND", "Signed by Placeholder Labs"
    return "NOT FOUND", "No C2PA manifest detected (stub)."


def _synthid_stub(image_bytes: bytes) -> tuple[str, str]:
    """Fake SynthID analyzer that hashes the first 32 bytes."""
    checksum = sum(image_bytes[:32]) % 5 if image_bytes else 0
    detected = checksum == 0
    status = "DETECTED" if detected else "NOT DETECTED"
    details = f"Checksum bucket={checksum} (demo only)."
    return status, details


def _human_consensus_stub(image_bytes: bytes) -> tuple[str, str]:
    """Fake human consensus analyzer showing vote placeholders."""
    pseudo_votes = len(image_bytes) % 7
    return "UNKNOWN", f"Votes — Real: {pseudo_votes}, AI: {max(0, 3 - pseudo_votes)} (stub)"


ANALYZERS: Sequence[AnalyzerSpec] = (
    AnalyzerSpec(name="Digital Signature (C2PA)", func=_digital_signature_stub),
    AnalyzerSpec(name="Google SynthID", func=_synthid_stub),
    AnalyzerSpec(name="Human Consensus", func=_human_consensus_stub),
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

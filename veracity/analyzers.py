
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, Future, as_completed
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

from .c2pa_analyzer import run_c2pa
from .human_analyzer import run_human_consensus
from .synthid_analyzer import run_synthid_stub

logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class AnalyzerSpec:
    name: str
    func: Callable[[bytes], tuple[str, str]]

ANALYZERS: Sequence[AnalyzerSpec] = (
    AnalyzerSpec(name="Digital Signature (C2PA)", func=run_c2pa),
    AnalyzerSpec(name="Google SynthID", func=run_synthid_stub),
    AnalyzerSpec(name="Human Consensus", func=run_human_consensus),
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

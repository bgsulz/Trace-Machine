import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, Future, as_completed
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

from flask import current_app

from .c2pa import run_c2pa
from .human import run_human_consensus
from .synthid import run_synthid_stub

logger = logging.getLogger(__name__)


AnalyzerOutput = dict[str, object]


@dataclass(frozen=True)
class AnalyzerSpec:
    name: str
    func: Callable[[bytes], AnalyzerOutput]


ANALYZERS: Sequence[AnalyzerSpec] = (
    AnalyzerSpec(name="Digital Signature (C2PA)", func=run_c2pa),
    AnalyzerSpec(name="Google SynthID", func=run_synthid_stub),
    AnalyzerSpec(name="Human Consensus", func=run_human_consensus),
)


def run_all_analyzers(
    image_bytes: bytes,
    analyzers: Iterable[AnalyzerSpec] | None = None,
) -> list[dict[str, object]]:
    """Execute all analyzers in parallel and normalize their outputs."""
    specs: Sequence[AnalyzerSpec]
    if analyzers is None:
        specs = tuple(ANALYZERS)
    else:
        specs = tuple(analyzers)

    if not specs:
        return []

    results_map: dict[str, AnalyzerOutput] = {}
    future_spec: dict[Future[AnalyzerOutput], AnalyzerSpec] = {}
    start_times: dict[Future[AnalyzerOutput], float] = {}

    max_workers = len(specs)
    logger.debug("Submitting %d analyzers using %d workers", len(specs), max_workers)
    app = current_app._get_current_object()

    with ThreadPoolExecutor(
        max_workers=max_workers, thread_name_prefix="analyzer"
    ) as executor:
        for spec in specs:
            logger.info("Analyzer '%s' starting", spec.name)
            future = executor.submit(_run_in_context, app, spec.func, image_bytes)
            future_spec[future] = spec
            start_times[future] = time.perf_counter()

        for future in as_completed(future_spec):
            spec = future_spec[future]
            elapsed = time.perf_counter() - start_times[future]
            try:
                raw = future.result()
            except Exception as exc:  # pragma: no cover - defensive logging
                logger.exception("Analyzer '%s' failed", spec.name)
                raw_dict: dict[str, object] = {
                    "status": "ERROR",
                    "summary": str(exc),
                    "data": {},
                }
            else:
                logger.info(
                    "Analyzer '%s' finished on %s in %.3fs",
                    spec.name,
                    threading.current_thread().name,
                    elapsed,
                )

                # Backwards compatibility: allow analyzers to return either
                # a dict or a (status, summary) tuple.
                if isinstance(raw, tuple) and len(raw) == 2:
                    status_val, summary_val = raw
                    raw_dict = {
                        "status": status_val,
                        "summary": summary_val,
                        "data": {},
                    }
                elif isinstance(raw, dict):
                    raw_dict = raw
                else:  # pragma: no cover - highly defensive
                    raw_dict = {
                        "status": "ERROR",
                        "summary": f"Unexpected analyzer return type: {type(raw)!r}",
                        "data": {},
                    }

            status = str(raw_dict.get("status", "UNKNOWN"))
            summary = str(raw_dict.get("summary", ""))
            data = raw_dict.get("data") or {}

            results_map[spec.name] = {
                "name": spec.name,
                "status": status,
                "summary": summary,
                "details": summary,
                "data": data,
            }

    return [results_map[spec.name] for spec in specs]


def _run_in_context(app, func: Callable[[bytes], AnalyzerOutput], payload: bytes) -> AnalyzerOutput:
    with app.app_context():
        return func(payload)

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, Future, as_completed
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence
from flask import current_app
from .context import AnalysisContext
from .c2pa import run_c2pa
from .exif import run_exif_metadata
from .human import run_human_consensus
from .synthid import run_synthid
from .tineye import get_tineye_status

logger = logging.getLogger(__name__)


AnalyzerOutput = dict[str, object]
DEFAULT_ANALYZER_TEMPLATE = "partials/analyzers/default.html"


@dataclass(frozen=True)
class AnalyzerSpec:
    name: str
    slug: str
    func: Callable[[AnalysisContext], AnalyzerOutput]
    template: str = DEFAULT_ANALYZER_TEMPLATE
    tooltip: str = ""


ANALYZERS: Sequence[AnalyzerSpec] = (
    AnalyzerSpec(
        name="Digital Signature (C2PA)",
        slug="c2pa",
        func=run_c2pa,
        template="partials/analyzers/c2pa.html",
        tooltip="Looks for authenticated Content Credentials attached to the file.",
    ),
    AnalyzerSpec(
        name="SynthID (Google)",
        slug="synthid",
        func=run_synthid,
        template="partials/analyzers/synthid.html",
        tooltip="Uses Google reverse image search to check for an invisible watermark.",
    ),
    AnalyzerSpec(
        name="TinEye Reverse Search",
        slug="tineye",
        func=get_tineye_status,
        template="partials/analyzers/tineye.html",
        tooltip="Searches TinEye for reverse image matches and checks against known AI sites.",
    ),
    AnalyzerSpec(
        name="AI Metadata (EXIF)",
        slug="exif",
        func=run_exif_metadata,
        template="partials/analyzers/exif.html",
        tooltip="Scans EXIF blocks for hints that common AI tools left behind.",
    ),
    AnalyzerSpec(
        name="Human Consensus",
        slug="human",
        func=run_human_consensus,
        template="partials/analyzers/human.html",
        tooltip="Compares this upload to prior submissions and shows community votes.",
    ),
)

_ANALYZER_BY_SLUG = {spec.slug: spec for spec in ANALYZERS}


def run_all_analyzers(
    context: AnalysisContext,
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
            future = executor.submit(_run_in_context, app, spec.func, context)
            future_spec[future] = spec
            start_times[future] = time.perf_counter()

        for future in as_completed(future_spec):
            spec = future_spec[future]
            elapsed = time.perf_counter() - start_times[future]
            try:
                raw = future.result()
            except Exception as exc:  # pragma: no cover - defensive logging
                logger.exception("Analyzer '%s' failed", spec.name)
                res = _error_payload(exc)
            else:
                logger.info(
                    "Analyzer '%s' finished on %s in %.3fs",
                    spec.name,
                    threading.current_thread().name,
                    elapsed,
                )
                res = raw

            results_map[spec.name] = _format_result(spec, res)

    return [results_map[spec.name] for spec in specs]


def _run_in_context(
    app, func: Callable[[AnalysisContext], AnalyzerOutput], payload: AnalysisContext
) -> AnalyzerOutput:
    with app.app_context():
        return func(payload)


def run_single_analyzer(context: AnalysisContext, slug: str) -> dict[str, object]:
    spec = _ANALYZER_BY_SLUG.get(slug)
    if spec is None:
        raise KeyError(f"Unknown analyzer slug: {slug}")

    logger.info("Analyzer '%s' starting (single run)", spec.name)
    start = time.perf_counter()
    try:
        raw = spec.func(context)
    except Exception as exc:  # pragma: no cover - defensive logging
        logger.exception("Analyzer '%s' failed", spec.name)
        res = _error_payload(exc)
    else:
        elapsed = time.perf_counter() - start
        logger.info("Analyzer '%s' finished in %.3fs", spec.name, elapsed)
        res = raw

    return _format_result(spec, res)


def get_analyzer_spec(slug: str) -> AnalyzerSpec | None:
    return _ANALYZER_BY_SLUG.get(slug)


def _error_payload(exc: Exception) -> dict[str, object]:
    return {
        "status": "ERROR",
        "summary": str(exc),
        "data": {},
    }


def _format_result(
    spec: AnalyzerSpec, raw_dict: dict[str, object]
) -> dict[str, object]:
    status = str(raw_dict.get("status", "UNKNOWN"))
    summary = str(raw_dict.get("summary", ""))
    data = raw_dict.get("data") or {}

    return {
        "name": spec.name,
        "slug": spec.slug,
        "status": status,
        "summary": summary,
        "details": summary,
        "data": data,
        "template": spec.template,
        "tooltip": spec.tooltip,
        "info_id": f"info-{spec.slug}",
    }

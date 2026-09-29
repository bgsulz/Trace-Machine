from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any
from urllib.parse import urlparse, quote_plus

from flask import abort, current_app, flash, redirect, render_template, url_for

from .. import ingestion
from ..autocrop import detect_overlay_crop
from ..analysis_cache import (
    load_analysis_metadata,
    load_analysis_payload,
    load_cached_analyzer_row,
    store_analysis_payload,
    store_cached_analyzer_row,
    update_analysis_metadata,
)
from ..analyzers.manager import (
    DEFAULT_ANALYZER_TEMPLATE,
    get_active_analyzers,
    get_analyzer_spec,
    run_all_analyzers,
    run_single_analyzer,
)
from .containment_service import get_displayable_containments
from .. import dethumbnail
from ..models import SynthIDReport, VoteHistory
from .remote_image_service import fetch_remote_image
from ..registry import prepare_analysis_context
from .trace_service import build_direct_and_distant_traces
from ..tools import generate_external_tools
from . import voting_service
from .synthid_service import PORTAL_RESULTS
from ..analyzers.human import _build_vote_breakdown


_SUMMARY_FALLBACK: dict[str, str] = {
    "manual": "Manual check required",
    "loading": "Running\u2026",
}


def _build_analyzer_summary(analysis_id: str) -> list[dict[str, Any]]:
    """Build the per-check overview from cached analyzer rows.

    TinEye rows are not persisted by default (compliance), so a missing TinEye
    row is shown as its on-demand manual state.
    """
    summary: list[dict[str, Any]] = []
    for spec in get_active_analyzers():
        row = load_cached_analyzer_row(analysis_id, spec.slug)
        if row is None:
            status = "MANUAL" if spec.slug == "tineye" else "LOADING"
            row = {"status": status, "summary": ""}
        status = (row.get("status") or "LOADING").upper()
        summary.append(
            {
                "slug": spec.slug,
                "name": spec.name,
                "status": status,
                "summary": row.get("summary") or _SUMMARY_FALLBACK.get(status.lower(), ""),
            }
        )
    return summary


def handle_remote_analysis(image_url: str, vote_slug: str | None, template_name: str):
    if not image_url:
        flash("Please provide an image URL to analyze.")
        return redirect(url_for("main.index"))

    try:
        fetched = fetch_remote_image(image_url)
    except ingestion.IngestionError as exc:
        flash(str(exc))
        return redirect(url_for("main.index"))

    if fetched.upgraded:
        flash("Upgraded from thumbnail to full resolution.")

    return perform_analysis(
        fetched.image_bytes,
        fetched.mime_type,
        "url",
        image_url=fetched.fetch_url,
        auto_vote=vote_slug,
        template_name=template_name,
    )


def perform_analysis(
    image_bytes: bytes,
    mime_type: str,
    source: str,
    *,
    image_url: str | None = None,
    auto_vote: str | None = None,
    template_name: str = "result.html",
    context=None,
    crop_box: tuple[float, float, float, float] | None = None,
):
    """Run every analyzer on *image_bytes* and persist the results.

    Full reports redirect to the analysis permalink (POST/redirect/GET) so
    results are shareable and refresh-safe. The mini report renders inline
    because it lives inside the browser extension's iframe.
    """
    context = context or prepare_analysis_context(image_bytes)
    phash = context.phash
    voting_service.persist_source_url(phash, image_url)
    _maybe_auto_vote(phash, auto_vote)

    public_url = image_url if source == "url" else None
    analysis_link = url_for("main.analyze", url=public_url) if public_url else None
    full_res_url = None
    if source == "url" and image_url:
        full_res_url = dethumbnail.get_full_res_url(image_url)
    public_url_display = _format_public_url(public_url)

    metadata: dict[str, Any] = {
        "mime_type": mime_type,
        "source": source,
        "image_url": image_url,
        "public_url": public_url,
        "analysis_link": analysis_link,
        "phash": context.phash,
        "whash": context.whash,
        "registry_id": context.registry_id,
        "crop_box": crop_box,
        "full_res_url": full_res_url,
        "image_width": context.width,
        "image_height": context.height,
        "public_url_display": public_url_display,
    }
    analysis_id = store_analysis_payload(None, image_bytes, metadata)
    analyzer_rows = _prime_analyzer_rows(analysis_id, context, get_active_analyzers())
    direct_distant = build_direct_and_distant_traces(
        context,
        analyzer_rows=analyzer_rows,
    )
    # Only run overlay detection when we aren't already looking at a cropped result,
    # to avoid suggesting a second auto-crop on an already-cropped image.
    auto_crop = detect_overlay_crop(image_bytes) if crop_box is None else None
    update_analysis_metadata(
        analysis_id,
        {
            "has_distant_traces": bool(
                direct_distant and direct_distant.get("distant_matches")
            ),
            "direct_distant": _json_safe(direct_distant),
            "auto_crop": _json_safe(asdict(auto_crop)) if auto_crop else None,
        },
    )

    if template_name == "result.html":
        return redirect(url_for("main.view_analysis", analysis_id=analysis_id))
    return render_analysis_page(analysis_id, template_name)


def render_analysis_page(analysis_id: str, template_name: str = "result.html"):
    """Render a stored analysis. Returns ``None`` when the analysis expired."""
    metadata = load_analysis_metadata(analysis_id)
    if metadata is None:
        return None

    public_url = metadata.get("public_url")
    active_analyzers = get_active_analyzers()
    registry_id = metadata.get("registry_id")
    containments = (
        get_displayable_containments(registry_id) if registry_id is not None else []
    )
    auto_crop = metadata.get("auto_crop") or None
    return render_template(
        template_name,
        image_url=url_for("main.serve_analysis_image", analysis_id=analysis_id),
        mime_type=metadata.get("mime_type"),
        source=metadata.get("source", "file"),
        analyzers=active_analyzers,
        invisible_watermarks_enabled=any(
            spec.slug == "invisible" for spec in active_analyzers
        ),
        tools=generate_external_tools(public_url, analysis_id=analysis_id),
        analysis_link=metadata.get("analysis_link"),
        analysis_id=analysis_id,
        registry_id=registry_id,
        containments=containments,
        direct_distant=metadata.get("direct_distant"),
        crop_box=metadata.get("crop_box"),
        full_res_url=metadata.get("full_res_url"),
        public_url=public_url,
        public_url_display=metadata.get("public_url_display"),
        image_width=metadata.get("image_width"),
        image_height=metadata.get("image_height"),
        analyzer_summary=_build_analyzer_summary(analysis_id),
        auto_crop=auto_crop,
    )


def _json_safe(value: Any) -> Any:
    """Round-trip through JSON so stored metadata matches what we reload."""
    return json.loads(json.dumps(value, default=str))


_MINI_TEMPLATES = {"c2pa", "exif", "human", "invisible", "synthid"}


def _should_persist_analyzer_row(slug: str) -> bool:
    if slug != "tineye":
        return True
    # TinEye compliance mode: strict mode avoids all analyzer-row persistence.
    return current_app.config.get("TINEYE_PERSISTENCE_MODE", "none") == "derived"


def render_analyzer_fragment_html(
    analysis_id: str,
    slug: str,
    *,
    link_target: str | None,
    refresh: bool = False,
    mini: bool = False,
):
    spec = get_analyzer_spec(slug)
    if spec is None:
        abort(404)
    if slug == "invisible" and spec not in get_active_analyzers():
        abort(404)

    payload = load_analysis_payload(analysis_id)
    metadata: dict[str, Any] | None = None

    if payload is None:
        row = _build_analyzer_error_row(spec, "Analysis expired. Please re-run.")
    else:
        image_bytes, metadata = payload
        should_persist = _should_persist_analyzer_row(slug)
        row = None if refresh or not should_persist else load_cached_analyzer_row(analysis_id, slug)
        if row is None:
            context = prepare_analysis_context(image_bytes)
            row = run_single_analyzer(context, slug)
            if should_persist:
                store_cached_analyzer_row(analysis_id, slug, row)

    _prepare_row_for_render(row, metadata, link_target, analysis_id)

    if mini and slug in _MINI_TEMPLATES:
        return render_template(f"partials/analyzers/mini/{slug}.html", row=row)
    return render_template("partials/analyzer_row.html", row=row)


def _prime_analyzer_rows(
    analysis_id: str,
    context,
    analyzers=None,
) -> list[dict[str, Any]]:
    rows = run_all_analyzers(context, analyzers=analyzers)
    for row in rows:
        slug = row.get("slug")
        if not slug:
            continue
        if not _should_persist_analyzer_row(str(slug)):
            continue
        store_cached_analyzer_row(analysis_id, slug, row)
    return rows


def _maybe_auto_vote(phash: str | None, auto_vote: str | None) -> None:
    if not auto_vote:
        return

    auto_vote_slug = auto_vote if auto_vote in {"real", "edited", "ai"} else None
    if not auto_vote_slug:
        return

    if not phash:
        flash("Automatic voting could not be completed for this image.")
        return

    voter_id = voting_service.get_voter_id()
    success, status = voting_service.apply_vote(phash, auto_vote_slug, voter_id)
    if not success:
        flash("Automatic voting is temporarily unavailable. Please try again.")
        return

    if status == "updated":
        flash("Vote updated automatically via shared link.")
    elif status == "unchanged":
        flash("This vote was already recorded for you; showing latest results.")
    else:
        flash("Vote recorded automatically via shared link.")


def _format_public_url(public_url: str | None, *, max_path_chars: int = 20) -> str | None:
    if not public_url:
        return None
    try:
        parsed = urlparse(public_url)
    except Exception:
        return public_url

    host = parsed.netloc or ""
    if not host:
        return public_url

    path = parsed.path.lstrip("/")
    if not path:
        return host

    trimmed = path[:max_path_chars]
    suffix = "..." if len(path) > max_path_chars else ""
    return f"{host}/{trimmed}{suffix}"


def _build_analyzer_error_row(spec, message: str) -> dict[str, Any]:
    return {
        "name": spec.name,
        "slug": spec.slug,
        "status": "ERROR",
        "summary": message,
        "data": {},
        "template": DEFAULT_ANALYZER_TEMPLATE,
    }


def _prepare_row_for_render(
    row: dict[str, Any],
    metadata: dict[str, Any] | None,
    link_target: str | None,
    analysis_id: str,
) -> None:
    metadata = metadata or {}
    row_data = dict(row.get("data") or {})
    phash = row_data.get("phash") or metadata.get("phash")
    if phash:
        row_data["phash"] = phash
    row["data"] = row_data
    _ensure_distant_match_flags(row, metadata)

    lens_link = _build_google_lens_link(metadata, analysis_id)
    search_tools = generate_external_tools(metadata.get("public_url"), analysis_id=analysis_id)

    row["context"] = {
        "source": metadata.get("source", "file"),
        "analysis_link": metadata.get("analysis_link"),
        "link_target": link_target,
        "analysis_id": analysis_id,
        "lens_link": lens_link,
        "search_links": search_tools[0]["links"] if search_tools else [],
    }

    slug = row.get("slug")

    if slug == "synthid":
        _ensure_synthid_detector_defaults(row_data)
        registry_id = metadata.get("registry_id")
        if registry_id is not None:
            _attach_synthid_report(row, registry_id)
        return

    if slug != "human":
        return

    registry_id = metadata.get("registry_id")
    if registry_id is None:
        return

    _ensure_human_vote_defaults(row_data)
    _attach_vote_history(row, registry_id)


def _attach_vote_history(row: dict[str, Any] | None, registry_id: int) -> None:
    if row is None:
        return

    history_row = VoteHistory.query.filter_by(
        image_id=registry_id,
        voter_id=voting_service.get_voter_id(),
    ).first()

    data = row.get("data") or {}
    data["current_vote"] = history_row.choice if history_row else None
    row["data"] = data


def _attach_synthid_report(row: dict[str, Any], registry_id: int) -> None:
    if row is None:
        return

    reports = SynthIDReport.query.filter_by(
        image_id=registry_id,
        voter_id=voting_service.get_voter_id(),
    ).all()

    data = row.get("data") or {}
    current_report = reports[0].result if reports else None
    data["current_report"] = current_report
    row["data"] = data


def _ensure_synthid_detector_defaults(row_data: dict[str, Any]) -> None:
    totals = row_data.get("totals")
    if not isinstance(totals, dict):
        totals = {}
    normalized_totals = {
        result: int(totals.get(result) or 0)
        for result in PORTAL_RESULTS
    }
    row_data["totals"] = normalized_totals
    row_data["checker_rows"] = [
        {
            "result": result,
            "provider": spec["provider"],
            "label": spec["label"],
            "short_label": spec["short_label"],
            "check_label": spec["check_label"],
            "count": normalized_totals[result],
        }
        for result, spec in PORTAL_RESULTS.items()
    ]


def _ensure_human_vote_defaults(row_data: dict[str, Any]) -> None:
    totals = row_data.get("totals")
    if not isinstance(totals, dict):
        totals = {}

    vote_real = int(totals.get("vote_real") or 0)
    vote_edited = int(totals.get("vote_edited") or 0)
    vote_ai = int(totals.get("vote_ai") or 0)

    row_totals = {
        "vote_real": vote_real,
        "vote_edited": vote_edited,
        "vote_ai": vote_ai,
        "total_votes": vote_real + vote_edited + vote_ai,
    }
    row_data["totals"] = row_totals

    breakdown = row_data.get("overall_breakdown")
    if not isinstance(breakdown, dict) or "segments" not in breakdown:
        row_data["overall_breakdown"] = _build_vote_breakdown(
            real=vote_real,
            edited=vote_edited,
            ai=vote_ai,
        )

    row_data.setdefault("matches", [])
    row_data.setdefault("matches_summary", "")
    row_data.setdefault("has_matches", False)
    row_data.setdefault("has_distant_matches", False)
    row_data.setdefault("distant_match_count", 0)
    row_data.setdefault("direct_only_message", "Votes are recorded on this image only.")
    row_data.setdefault("local_match_count", 0)
    row_data.setdefault("no_votes_message", "No votes yet.")


def _ensure_distant_match_flags(row: dict[str, Any], metadata: dict[str, Any] | None) -> None:
    row_data = row.get("data")
    if not isinstance(row_data, dict):
        return

    slug = str(row.get("slug") or "")
    if slug == "c2pa":
        row_data["has_distant_matches"] = bool(row_data.get("matches") or [])
        return

    if slug == "synthid":
        row_data["has_distant_matches"] = bool(row_data.get("similar_images") or [])
        return

    if slug == "invisible":
        row_data["has_distant_matches"] = False
        return

    if slug == "exif":
        row_data["has_distant_matches"] = False
        return

    if slug != "human":
        return

    matches = row_data.get("matches") or []
    if not isinstance(matches, list):
        row_data["has_distant_matches"] = False
        row_data["distant_match_count"] = 0
        return

    registry_id = metadata.get("registry_id") if isinstance(metadata, dict) else None
    row_phash = str(row_data.get("phash") or "")
    row_whash = str(row_data.get("whash") or (metadata or {}).get("whash") or "")
    distant_match_count = sum(
        1
        for match in matches
        if isinstance(match, dict)
        and not _is_human_self_match(
            match,
            registry_id=registry_id,
            row_phash=row_phash,
            row_whash=row_whash,
        )
    )
    row_data["distant_match_count"] = distant_match_count
    row_data["has_distant_matches"] = distant_match_count > 0


def _is_human_self_match(
    match: dict[str, Any],
    *,
    registry_id: int | None,
    row_phash: str,
    row_whash: str,
) -> bool:
    explicit = match.get("is_self_match")
    if isinstance(explicit, bool):
        return explicit

    image_id = match.get("image_id")
    if registry_id is not None and image_id is not None:
        return image_id == registry_id

    match_phash = str(match.get("phash") or "")
    match_whash = str(match.get("whash") or "")
    if not row_phash or not match_phash or row_phash != match_phash:
        return False

    if row_whash and match_whash and row_whash != match_whash:
        return False
    return True


def _build_google_lens_link(metadata: dict[str, Any] | None, analysis_id: str | None) -> str | None:
    metadata = metadata or {}
    public_url = metadata.get("public_url")
    target_url = public_url

    if not target_url and analysis_id:
        target_url = url_for("main.serve_analysis_image", analysis_id=analysis_id, _external=True)

    if not target_url:
        return "https://images.google.com/"

    encoded = quote_plus(target_url)
    return f"https://lens.google.com/upload?url={encoded}"


from __future__ import annotations

import base64
from typing import Any

from flask import abort, flash, redirect, render_template, url_for

from . import ingestion
from .analysis_cache import (
    load_analysis_payload,
    load_cached_analyzer_row,
    store_analysis_payload,
    store_cached_analyzer_row,
)
from .analyzers.manager import (
    ANALYZERS,
    DEFAULT_ANALYZER_TEMPLATE,
    get_analyzer_spec,
    run_all_analyzers,
    run_single_analyzer,
)
from .containment_service import get_displayable_containments
from .models import VoteHistory
from .registry import prepare_analysis_context
from .tools import generate_external_tools
from . import voting_service


def handle_remote_analysis(image_url: str, vote_slug: str | None, template_name: str):
    if not image_url:
        flash("Please provide an image URL to analyze.")
        return redirect(url_for("main.index"))

    try:
        image_bytes, mime_type = ingestion.fetch_image_bytes(image_url)
    except ingestion.IngestionError as exc:
        flash(str(exc))
        return redirect(url_for("main.index"))

    return perform_analysis(
        image_bytes,
        mime_type,
        "url",
        image_url=image_url,
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
):
    image_data_url = _build_image_data_url(image_bytes, mime_type)

    context = context or prepare_analysis_context(image_bytes)
    phash = context.phash
    voting_service.persist_source_url(phash, image_url)
    _maybe_auto_vote(phash, auto_vote)

    public_url = image_url if source == "url" else None
    analysis_link = url_for("main.analyze", url=public_url) if public_url else None
    metadata: dict[str, Any] = {
        "mime_type": mime_type,
        "source": source,
        "image_url": image_url,
        "public_url": public_url,
        "analysis_link": analysis_link,
        "phash": context.phash,
        "whash": context.whash,
        "registry_id": context.registry_id,
    }
    analysis_id = store_analysis_payload(None, image_bytes, metadata)
    tool_results = generate_external_tools(public_url, analysis_id=analysis_id)
    _prime_analyzer_rows(analysis_id, context)
    containments = get_displayable_containments(context.registry_id)
    return render_template(
        template_name,
        image_url=image_data_url,
        source=source,
        analyzers=ANALYZERS,
        tools=tool_results,
        analysis_link=analysis_link,
        analysis_id=analysis_id,
        registry_id=context.registry_id,
        containments=containments,
    )


def render_analyzer_fragment_html(
    analysis_id: str,
    slug: str,
    *,
    link_target: str | None,
    refresh: bool = False,
):
    spec = get_analyzer_spec(slug)
    if spec is None:
        abort(404)

    payload = load_analysis_payload(analysis_id)
    metadata: dict[str, Any] | None = None

    if payload is None:
        row = _build_analyzer_error_row(spec, "Analysis expired. Please re-run.")
    else:
        image_bytes, metadata = payload
        row = None if refresh else load_cached_analyzer_row(analysis_id, slug)
        if row is None:
            context = prepare_analysis_context(image_bytes)
            row = run_single_analyzer(context, slug)
            store_cached_analyzer_row(analysis_id, slug, row)

    _prepare_row_for_render(row, metadata, link_target, analysis_id)
    return render_template("partials/analyzer_row.html", row=row)


def _prime_analyzer_rows(analysis_id: str, context) -> None:
    rows = run_all_analyzers(context)
    for row in rows:
        slug = row.get("slug")
        if not slug:
            continue
        store_cached_analyzer_row(analysis_id, slug, row)


def _build_image_data_url(image_bytes: bytes, mime_type: str) -> str:
    image_b64 = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{mime_type};base64,{image_b64}"


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

    row["context"] = {
        "source": metadata.get("source", "file"),
        "analysis_link": metadata.get("analysis_link"),
        "link_target": link_target,
        "analysis_id": analysis_id,
    }

    if row.get("slug") != "human":
        return

    registry_id = metadata.get("registry_id")
    if registry_id is None:
        return

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

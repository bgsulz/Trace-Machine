from __future__ import annotations

import json
from collections.abc import Callable

from flask import Blueprint, abort, current_app, flash, jsonify, make_response, redirect, request, url_for

from ... import csrf, limiter
from ...analysis_cache import load_analysis_payload
from ...services.analysis_service import render_analyzer_fragment_html
from ...services.config_service import increment_total_donated, parse_amount_to_cents
from ...models import ProvenanceFact
from ...services import openai_verify_service
from ...services.synthid_service import apply_synthid_report, normalize_portal_result
from ...services.voting_service import VOTE_CHOICES, apply_vote, get_voter_id


def _is_htmx_request() -> bool:
    return bool(request.headers.get("HX-Request"))


def _refresh_analyzer_fragment(
    analysis_id: str,
    slug: str,
    *,
    mini: bool,
    link_target: str | None,
) -> str:
    return render_analyzer_fragment_html(
        analysis_id,
        slug,
        link_target=link_target,
        refresh=True,
        mini=mini,
    )


def _toast_response(html: str, message: str):
    response = make_response(html)
    response.headers["HX-Trigger"] = json.dumps({"showToast": message})
    return response


def register_community_routes(
    bp: Blueprint,
    expired_analysis_response: Callable[[], object],
) -> None:
    @bp.route("/vote", methods=["POST"])
    def vote():
        phash = (request.form.get("phash") or "").strip()
        vote_kind = (request.form.get("vote") or "").strip().lower()
        source_type = (request.form.get("source_type") or "").strip().lower()
        analysis_link = (request.form.get("analysis_link") or "").strip()
        analysis_id = (request.form.get("analysis_id") or "").strip()
        link_target = (request.form.get("link_target") or "").strip()
        mini = request.form.get("mini") == "1"

        if not phash or vote_kind not in VOTE_CHOICES:
            flash("Invalid vote request.")
            return redirect(url_for("main.index"))

        voter_id = get_voter_id()
        success, status = apply_vote(phash, vote_kind, voter_id)
        if not success:
            flash("Voting is temporarily unavailable. Please try again.")
            return redirect(url_for("main.index"))

        redirect_target = url_for("main.index")
        if source_type == "url" and analysis_link.startswith("/"):
            redirect_target = analysis_link
        if _is_htmx_request() and analysis_id:
            payload = load_analysis_payload(analysis_id)
            if payload is None:
                return expired_analysis_response()
            html = _refresh_analyzer_fragment(
                analysis_id,
                "human",
                mini=mini,
                link_target=link_target or None,
            )
            return _toast_response(html, "Thanks for your vote.")
        flash("Thanks for your vote.")
        return redirect(redirect_target)

    @bp.route("/synthid-report", methods=["POST"])
    def synthid_report():
        report = (request.form.get("report") or "").strip().lower()
        provider = (request.form.get("provider") or "google").strip().lower()
        detector = (
            request.form.get("detector") or "google_about_this_image"
        ).strip().lower()
        analysis_id = (request.form.get("analysis_id") or "").strip()
        mini = request.form.get("mini") == "1"
        if not analysis_id:
            if _is_htmx_request():
                return expired_analysis_response()
            flash("Invalid report request.")
            return redirect(url_for("main.index"))

        payload = load_analysis_payload(analysis_id)
        if payload is None:
            return expired_analysis_response()
        _, metadata = payload
        phash = (metadata.get("phash") or "").strip()

        normalized_report = normalize_portal_result(
            report,
            provider=provider,
            detector=detector,
        )
        if not phash or normalized_report is None:
            flash("Invalid report request.")
            return redirect(url_for("main.index"))

        voter_id = get_voter_id()
        success, status = apply_synthid_report(
            phash,
            normalized_report,
            voter_id,
            provider=provider,
            detector=detector,
        )
        if not success:
            flash("Reporting is temporarily unavailable. Please try again.")
            return redirect(url_for("main.index"))

        if _is_htmx_request() and analysis_id:
            html = _refresh_analyzer_fragment(
                analysis_id,
                "synthid",
                mini=mini,
                link_target="_blank" if mini else None,
            )
            msg = "Portal report recorded." if status == "recorded" else "Portal report updated."
            if status == "unchanged":
                msg = "You already submitted this report."
            return _toast_response(html, msg)

        flash("Thanks for your report.")
        return redirect(url_for("main.index"))

    @bp.route("/analysis/<analysis_id>/openai-check", methods=["POST"])
    @limiter.limit("10/hour")
    @limiter.limit("300/day", key_func=lambda: "openai-check-global")
    def openai_check(analysis_id: str):
        """Run OpenAI's provenance API on this image (on demand)."""
        if not openai_verify_service.is_configured():
            abort(404)
        payload = load_analysis_payload(analysis_id)
        if payload is None:
            return expired_analysis_response()
        image_bytes, metadata = payload

        def notice(message: str):
            if _is_htmx_request():
                response = make_response("", 200)
                response.headers["HX-Reswap"] = "none"
                response.headers["HX-Trigger"] = json.dumps({"showToast": message})
                return response
            flash(message)
            return redirect(url_for("main.view_analysis", analysis_id=analysis_id))

        if not openai_verify_service.may_check(analysis_id, metadata):
            return notice("Only the person who uploaded this image can send it to OpenAI.")
        registry_id = metadata.get("registry_id")
        if registry_id is None:
            return notice("This image can't be checked automatically.")

        mime_type = metadata.get("mime_type", "application/octet-stream")
        converted = mime_type not in openai_verify_service.SUPPORTED_MIME_TYPES
        facts = ProvenanceFact.query.filter_by(
            image_id=registry_id, analyzer=openai_verify_service.FACT_ANALYZER
        ).all()
        result = openai_verify_service.recent_result(facts, converted=converted)
        reused = result is not None
        if not reused:
            try:
                result = openai_verify_service.run_check(image_bytes, mime_type)
            except openai_verify_service.OpenAIVerifyError as exc:
                return notice(str(exc))
            openai_verify_service.record(registry_id, result)

        if not _is_htmx_request():
            return redirect(url_for("main.view_analysis", analysis_id=analysis_id))
        html = _refresh_analyzer_fragment(analysis_id, "synthid", mini=False, link_target=None)
        if reused:
            message = "This image was checked recently; showing that result."
        elif result["detected"]:
            message = "OpenAI's API found OpenAI signals in this image."
        else:
            message = "OpenAI's API found no OpenAI signals."
        return _toast_response(html, message)

    @bp.route("/webhooks/kofi", methods=["POST"])
    @csrf.exempt
    def kofi_webhook():
        payload = request.get_json(silent=True) or {}
        if not payload:
            raw = request.form.get("data")
            if raw:
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    payload = {}
        provided_token = (payload.get("verification_token") or "").strip()
        expected_token = current_app.config.get("KOFI_TOKEN", "").strip()
        if not expected_token or provided_token != expected_token:
            abort(403)

        amount_cents = parse_amount_to_cents(payload.get("amount"))
        config = increment_total_donated(amount_cents)

        return jsonify(
            {
                "status": "ok",
                "added_cents": amount_cents,
                "total_cents": config.total_donated_cents,
            }
        )

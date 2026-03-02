from __future__ import annotations

import json
from collections.abc import Callable

from flask import Blueprint, abort, current_app, flash, jsonify, make_response, redirect, request, url_for

from ... import csrf
from ...analysis_cache import load_analysis_payload
from ...services.analysis_service import render_analyzer_fragment_html, render_evidence_summary_oob
from ...services.config_service import increment_total_donated, parse_amount_to_cents
from ...services.synthid_service import SYNTHID_CHOICES, apply_synthid_report
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
            html += render_evidence_summary_oob(analysis_id)
            return _toast_response(html, "Thanks for your vote.")
        flash("Thanks for your vote.")
        return redirect(redirect_target)

    @bp.route("/synthid-report", methods=["POST"])
    def synthid_report():
        report = (request.form.get("report") or "").strip().lower()
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

        if not phash or report not in SYNTHID_CHOICES:
            flash("Invalid report request.")
            return redirect(url_for("main.index"))

        voter_id = get_voter_id()
        success, status = apply_synthid_report(phash, report, voter_id)
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
            html += render_evidence_summary_oob(analysis_id)
            msg = "SynthID report recorded." if status == "recorded" else "SynthID report updated."
            if status == "unchanged":
                msg = "You already submitted this report."
            return _toast_response(html, msg)

        flash("Thanks for your report.")
        return redirect(url_for("main.index"))

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

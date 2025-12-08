from flask import Blueprint, flash, redirect, render_template, request, url_for

from . import ingestion
from .analysis_service import (
    handle_remote_analysis,
    perform_analysis,
    render_analyzer_fragment_html,
)
from .voting_service import VOTE_CHOICES, apply_vote, get_voter_id

bp = Blueprint("main", __name__)


@bp.route("/")
def index():
    return render_template("index.html")


@bp.route("/analysis/<analysis_id>/analyzers/<slug>")
def analyzer_fragment(analysis_id: str, slug: str):
    link_target = "_blank" if request.args.get("mini") == "1" else None
    return render_analyzer_fragment_html(
        analysis_id,
        slug,
        link_target=link_target,
    )


@bp.route("/analyze", methods=["GET", "POST"])
def analyze():
    if request.method == "GET":
        image_url = (request.args.get("url") or "").strip()
        vote_slug = (request.args.get("vote") or "").strip().lower()
        if vote_slug not in VOTE_CHOICES:
            vote_slug = None

        return handle_remote_analysis(image_url, vote_slug, "result.html")

    # POST: form submission
    file = request.files.get("file")
    image_url = (request.form.get("image_url") or "").strip()

    # If a URL was provided without a file, redirect to the GET endpoint so
    # the resulting page has a shareable /analyze?url=... link.
    if (not file or not file.filename) and image_url:
        return redirect(url_for("main.analyze", url=image_url))

    # Otherwise, handle file uploads as before.
    try:
        if file and file.filename:
            source = "file"
            image_bytes = file.read()
            ingestion.validate_image_bytes(image_bytes)
            mime_type = file.mimetype or "application/octet-stream"
        else:
            flash("Please provide an image file or a URL.")
            return redirect(url_for("main.index"))
    except ingestion.IngestionError as exc:
        flash(str(exc))
        return redirect(url_for("main.index"))

    return perform_analysis(image_bytes, mime_type, source)


@bp.route("/analyze-mini")
def analyze_mini():
    image_url = (request.args.get("url") or "").strip()
    vote_slug = (request.args.get("vote") or "").strip().lower()
    if vote_slug not in VOTE_CHOICES:
        vote_slug = None

    return handle_remote_analysis(image_url, vote_slug, "result_mini.html")


@bp.route("/vote", methods=["POST"])
def vote():
    phash = (request.form.get("phash") or "").strip()
    vote_kind = (request.form.get("vote") or "").strip().lower()
    source_type = (request.form.get("source_type") or "").strip().lower()
    analysis_link = (request.form.get("analysis_link") or "").strip()
    analysis_id = (request.form.get("analysis_id") or "").strip()
    link_target = (request.form.get("link_target") or "").strip()

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
    if request.headers.get("HX-Request") and analysis_id:
        return render_analyzer_fragment_html(
            analysis_id,
            "human",
            link_target=link_target or None,
            refresh=True,
        )
    flash("Thanks for your vote.")
    return redirect(redirect_target)

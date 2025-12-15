from io import BytesIO
import json

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from . import csrf, ingestion
from .analysis_service import (
    handle_remote_analysis,
    perform_analysis,
    render_analyzer_fragment_html,
)
from .analyzers.manager import ANALYZERS
from .analysis_cache import load_analysis_payload
from .config_service import (
    DONATION_GOAL_CENTS,
    get_global_config,
    increment_total_donated,
    parse_amount_to_cents,
)
from .voting_service import VOTE_CHOICES, apply_vote, get_voter_id

bp = Blueprint("main", __name__)


@bp.route("/")
def index():
    config = get_global_config()
    total_cents = config.total_donated_cents
    progress = min(total_cents / DONATION_GOAL_CENTS, 1) if DONATION_GOAL_CENTS else 0
    return render_template(
        "index.html",
        donation_total_cents=total_cents,
        donation_goal_cents=DONATION_GOAL_CENTS,
        donation_progress_percent=round(progress * 100, 2),
        donation_goal_met=total_cents >= DONATION_GOAL_CENTS,
    )


@bp.route("/info")
def analyzer_info():
    return render_template("info.html", analyzers=ANALYZERS)


@bp.route("/analysis/<analysis_id>/analyzers/<slug>")
def analyzer_fragment(analysis_id: str, slug: str):
    link_target = "_blank" if request.args.get("mini") == "1" else None
    return render_analyzer_fragment_html(
        analysis_id,
        slug,
        link_target=link_target,
    )


@bp.route("/analysis/<analysis_id>/raw")
def serve_analysis_image(analysis_id: str):
    """
    Serves the raw image bytes for a given analysis ID.
    Required for external tools (Google/Bing) to 'see' our uploaded file.
    """
    payload = load_analysis_payload(analysis_id)
    if payload is None:
        abort(404)

    image_bytes, metadata = payload
    mime_type = metadata.get("mime_type", "application/octet-stream")

    return send_file(BytesIO(image_bytes), mimetype=mime_type, max_age=3600)


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

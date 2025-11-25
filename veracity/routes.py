import base64
import hashlib

from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask import current_app
from sqlalchemy.exc import IntegrityError

from . import ingestion, db
from .models import ImageConsensus, VoteHistory, ImageSource, ImageRegistry
from .registry import prepare_analysis_context
from .analyzers.manager import run_all_analyzers

bp = Blueprint("main", __name__)


@bp.route("/")
def index():
    return render_template("index.html")


def _perform_analysis(
    image_bytes: bytes, mime_type: str, source: str, image_url: str | None = None
):
    image_b64 = base64.b64encode(image_bytes).decode("ascii")
    image_data_url = f"data:{mime_type};base64,{image_b64}"

    context = prepare_analysis_context(image_bytes)
    analyzer_results = run_all_analyzers(context)

    # Persist the mapping from Human Consensus phash -> source URL so
    # that future analyses can link back to this image by URL.
    if image_url:
        human_row = next(
            (row for row in analyzer_results if row.get("slug") == "human"),
            None,
        )
        phash = None
        if human_row is not None:
            data = human_row.get("data") or {}
            phash = data.get("phash")

        if phash and image_url:
            # Look up the registry row for this perceptual hash so we can
            # associate the source URL with the canonical image record.
            registry_row = ImageRegistry.query.filter_by(phash=phash).first()
            if registry_row is None:
                registry_row = ImageRegistry(phash=phash)
                db.session.add(registry_row)
                db.session.flush()

            record = ImageSource(image_id=registry_row.id, url=image_url)
            db.session.add(record)
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()

    return render_template(
        "result.html",
        image_url=image_data_url,
        source=source,
        results=analyzer_results,
    )


@bp.route("/analyze", methods=["GET", "POST"])
def analyze():
    if request.method == "GET":
        image_url = (request.args.get("url") or "").strip()

        if not image_url:
            # No URL provided in query string; nothing to analyze.
            flash("Please provide an image URL to analyze.")
            return redirect(url_for("main.index"))

        try:
            image_bytes, mime_type = ingestion.fetch_image_bytes(image_url)
        except ingestion.IngestionError as exc:
            flash(str(exc))
            return redirect(url_for("main.index"))

        return _perform_analysis(image_bytes, mime_type, "url", image_url=image_url)

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

    return _perform_analysis(image_bytes, mime_type, source)


@bp.route("/vote", methods=["POST"])
def vote():
    phash = (request.form.get("phash") or "").strip()
    vote_kind = (request.form.get("vote") or "").strip().lower()

    if not phash or vote_kind not in {"real", "edited", "ai"}:
        flash("Invalid vote request.")
        return redirect(url_for("main.index"))

    voter_id = _build_voter_id(_get_client_ip())

    # Resolve or create the ImageRegistry row for this perceptual hash so we
    # can store vote history and consensus against a stable image_id.
    registry_row = ImageRegistry.query.filter_by(phash=phash).first()
    if registry_row is None:
        registry_row = ImageRegistry(phash=phash)
        db.session.add(registry_row)
        db.session.flush()

    history_row = VoteHistory(image_id=registry_row.id, voter_id=voter_id)
    db.session.add(history_row)
    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        flash("You have already voted on this image.")
        return redirect(url_for("main.index"))

    record = ImageConsensus.query.filter_by(image_id=registry_row.id).first()
    if record is None:
        record = ImageConsensus(image_id=registry_row.id)
        db.session.add(record)

    _increment_vote_counts(record, vote_kind)

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()

        record = ImageConsensus.query.filter_by(image_id=registry_row.id).first()
        if record is None:
            flash("Voting is temporarily unavailable. Please try again.")
            return redirect(url_for("main.index"))

        _increment_vote_counts(record, vote_kind)

        db.session.commit()

    flash("Thanks for your vote.")
    return redirect(url_for("main.index"))


def _get_client_ip() -> str:
    forwarded = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
    if forwarded:
        return forwarded
    return request.remote_addr or "unknown"


def _increment_vote_counts(record: ImageConsensus, vote_kind: str) -> None:
    if vote_kind == "real":
        record.vote_real = (record.vote_real or 0) + 1
    elif vote_kind == "edited":
        record.vote_edited = (record.vote_edited or 0) + 1
    else:
        record.vote_ai = (record.vote_ai or 0) + 1


def _build_voter_id(ip_address: str) -> str:
    secret = current_app.config.get("SECRET_KEY", "")
    payload = f"{ip_address}:{secret}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()

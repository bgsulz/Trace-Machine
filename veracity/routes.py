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
from .tools import generate_external_tools

bp = Blueprint("main", __name__)


@bp.route("/")
def index():
    return render_template("index.html")


def _perform_analysis(
    image_bytes: bytes,
    mime_type: str,
    source: str,
    image_url: str | None = None,
    auto_vote: str | None = None,
    template_name: str = "result.html",
):
    image_b64 = base64.b64encode(image_bytes).decode("ascii")
    image_data_url = f"data:{mime_type};base64,{image_b64}"

    context = prepare_analysis_context(image_bytes)
    analyzer_results = run_all_analyzers(context)

    human_row = next(
        (row for row in analyzer_results if row.get("slug") == "human"), None
    )
    phash = None
    if human_row is not None:
        data = human_row.get("data") or {}
        human_row["data"] = data
        phash = data.get("phash")

    # Persist the mapping from Human Consensus phash -> source URL so
    # that future analyses can link back to this image by URL.
    if image_url and phash:
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

    voter_id = _build_voter_id(_get_client_ip())

    auto_vote_slug = auto_vote if auto_vote in {"real", "edited", "ai"} else None
    if auto_vote_slug:
        if phash:
            success, status = _apply_vote(phash, auto_vote_slug, voter_id)
            if success:
                if status == "updated":
                    flash("Vote updated automatically via shared link.")
                elif status == "unchanged":
                    flash(
                        "This vote was already recorded for you; showing latest results."
                    )
                else:
                    flash("Vote recorded automatically via shared link.")
            else:
                flash("Automatic voting is temporarily unavailable. Please try again.")
        else:
            flash("Automatic voting could not be completed for this image.")

    if human_row is not None:
        history_row = VoteHistory.query.filter_by(
            image_id=context.registry_id,
            voter_id=voter_id,
        ).first()

        data = human_row.get("data") or {}
        data["current_vote"] = history_row.choice if history_row else None
        human_row["data"] = data

    public_url = image_url if source == "url" else None
    tool_results = generate_external_tools(public_url)

    return render_template(
        template_name,
        image_url=image_data_url,
        source=source,
        results=analyzer_results,
        tools=tool_results,
    )


@bp.route("/analyze", methods=["GET", "POST"])
def analyze():
    if request.method == "GET":
        image_url = (request.args.get("url") or "").strip()
        vote_slug = (request.args.get("vote") or "").strip().lower()
        if vote_slug not in {"real", "edited", "ai"}:
            vote_slug = None

        if not image_url:
            # No URL provided in query string; nothing to analyze.
            flash("Please provide an image URL to analyze.")
            return redirect(url_for("main.index"))

        try:
            image_bytes, mime_type = ingestion.fetch_image_bytes(image_url)
        except ingestion.IngestionError as exc:
            flash(str(exc))
            return redirect(url_for("main.index"))

        return _perform_analysis(
            image_bytes,
            mime_type,
            "url",
            image_url=image_url,
            auto_vote=vote_slug,
        )

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


@bp.route("/analyze-mini")
def analyze_mini():
    image_url = (request.args.get("url") or "").strip()
    vote_slug = (request.args.get("vote") or "").strip().lower()
    if vote_slug not in {"real", "edited", "ai"}:
        vote_slug = None

    if not image_url:
        flash("Please provide an image URL to analyze.")
        return redirect(url_for("main.index"))

    try:
        image_bytes, mime_type = ingestion.fetch_image_bytes(image_url)
    except ingestion.IngestionError as exc:
        flash(str(exc))
        return redirect(url_for("main.index"))

    return _perform_analysis(
        image_bytes,
        mime_type,
        "url",
        image_url=image_url,
        auto_vote=vote_slug,
        template_name="result_mini.html",
    )


@bp.route("/vote", methods=["POST"])
def vote():
    phash = (request.form.get("phash") or "").strip()
    vote_kind = (request.form.get("vote") or "").strip().lower()

    if not phash or vote_kind not in {"real", "edited", "ai"}:
        flash("Invalid vote request.")
        return redirect(url_for("main.index"))

    voter_id = _build_voter_id(_get_client_ip())
    success, status = _apply_vote(phash, vote_kind, voter_id)
    if not success:
        flash("Voting is temporarily unavailable. Please try again.")
        return redirect(url_for("main.index"))

    flash("Thanks for your vote.")
    return redirect(url_for("main.index"))


def _apply_vote(phash: str, vote_kind: str, voter_id: str) -> tuple[bool, str | None]:
    if vote_kind not in {"real", "edited", "ai"}:
        return False, None

    # Resolve or create the ImageRegistry row for this perceptual hash so we
    # can store vote history and consensus against a stable image_id.
    registry_row = ImageRegistry.query.filter_by(phash=phash).first()
    if registry_row is None:
        registry_row = ImageRegistry(phash=phash)
        db.session.add(registry_row)
        db.session.flush()

    record = ImageConsensus.query.filter_by(image_id=registry_row.id).first()
    if record is None:
        record = ImageConsensus(image_id=registry_row.id)
        db.session.add(record)

    history_row = VoteHistory.query.filter_by(
        image_id=registry_row.id,
        voter_id=voter_id,
    ).first()

    status = "unchanged"
    if history_row is None:
        history_row = VoteHistory(
            image_id=registry_row.id,
            voter_id=voter_id,
            choice=vote_kind,
        )
        db.session.add(history_row)
        _increment_vote_counts(record, vote_kind)
        status = "recorded"
    else:
        previous_choice = history_row.choice
        if previous_choice != vote_kind:
            _decrement_vote_counts(record, previous_choice)
            _increment_vote_counts(record, vote_kind)
            history_row.choice = vote_kind
            status = "updated"

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return False, None

    return True, status


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


def _decrement_vote_counts(record: ImageConsensus, vote_kind: str) -> None:
    if vote_kind == "real":
        record.vote_real = max((record.vote_real or 0) - 1, 0)
    elif vote_kind == "edited":
        record.vote_edited = max((record.vote_edited or 0) - 1, 0)
    else:
        record.vote_ai = max((record.vote_ai or 0) - 1, 0)


def _build_voter_id(ip_address: str) -> str:
    secret = current_app.config.get("SECRET_KEY", "")
    payload = f"{ip_address}:{secret}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()

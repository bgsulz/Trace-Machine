import base64
import hashlib
import json
import time
import uuid
from pathlib import Path

from flask import (
    Blueprint,
    abort,
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
from .analyzers.manager import (
    ANALYZERS,
    DEFAULT_ANALYZER_TEMPLATE,
    run_all_analyzers,
    get_analyzer_spec,
    run_single_analyzer,
)
from .tools import generate_external_tools

bp = Blueprint("main", __name__)

_ANALYSIS_DIRNAME = "analysis_cache"
_ANALYSIS_BYTES_SUFFIX = ".bin"
_ANALYSIS_META_SUFFIX = ".json"


@bp.route("/")
def index():
    return render_template("index.html")


def _analysis_dir() -> Path:
    base = Path(current_app.instance_path) / _ANALYSIS_DIRNAME
    base.mkdir(parents=True, exist_ok=True)
    return base


def _analysis_bytes_path(analysis_id: str) -> Path:
    return _analysis_dir() / f"{analysis_id}{_ANALYSIS_BYTES_SUFFIX}"


def _analysis_meta_path(analysis_id: str) -> Path:
    return _analysis_dir() / f"{analysis_id}{_ANALYSIS_META_SUFFIX}"


def _analysis_row_path(analysis_id: str, slug: str) -> Path:
    return _analysis_dir() / f"{analysis_id}-{slug}.row.json"


def _store_analysis_payload(
    analysis_id: str | None,
    image_bytes: bytes,
    metadata: dict[str, object],
) -> str:
    token = analysis_id or uuid.uuid4().hex
    data_path = _analysis_bytes_path(token)
    meta_path = _analysis_meta_path(token)
    data_path.write_bytes(image_bytes)
    metadata = {**metadata, "created_at": time.time()}
    meta_path.write_text(json.dumps(metadata), encoding="utf-8")
    return token


def _load_analysis_payload(
    analysis_id: str,
) -> tuple[bytes, dict[str, object]] | None:
    data_path = _analysis_bytes_path(analysis_id)
    meta_path = _analysis_meta_path(analysis_id)
    try:
        image_bytes = data_path.read_bytes()
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    return image_bytes, metadata


def _perform_analysis(
    image_bytes: bytes,
    mime_type: str,
    source: str,
    image_url: str | None = None,
    auto_vote: str | None = None,
    template_name: str = "result.html",
):
    image_data_url = _build_image_data_url(image_bytes, mime_type)

    context = prepare_analysis_context(image_bytes)
    phash = context.phash
    _persist_source_url(phash, image_url)
    voter_id = _build_voter_id(_get_client_ip())
    _maybe_auto_vote(phash, auto_vote, voter_id)

    public_url = image_url if source == "url" else None
    tool_results = generate_external_tools(public_url)
    analysis_link = url_for("main.analyze", url=public_url) if public_url else None
    metadata = {
        "mime_type": mime_type,
        "source": source,
        "image_url": image_url,
        "public_url": public_url,
        "analysis_link": analysis_link,
        "phash": context.phash,
        "whash": context.whash,
        "registry_id": context.registry_id,
    }
    analysis_id = _store_analysis_payload(None, image_bytes, metadata)
    _prime_analyzer_rows(analysis_id, context)
    return render_template(
        template_name,
        image_url=image_data_url,
        source=source,
        analyzers=ANALYZERS,
        tools=tool_results,
        analysis_link=analysis_link,
        analysis_id=analysis_id,
    )


@bp.route("/analysis/<analysis_id>/analyzers/<slug>")
def analyzer_fragment(analysis_id: str, slug: str):
    spec = get_analyzer_spec(slug)
    if spec is None:
        abort(404)

    payload = _load_analysis_payload(analysis_id)
    link_target = "_blank" if request.args.get("mini") == "1" else None
    metadata: dict[str, object] | None = None

    if payload is None:
        row = _build_analyzer_error_row(spec, "Analysis expired. Please re-run.")
    else:
        image_bytes, metadata = payload
        row = _load_cached_analyzer_row(analysis_id, slug)
        if row is None:
            context = prepare_analysis_context(image_bytes)
            row = run_single_analyzer(context, slug)
            _store_cached_analyzer_row(analysis_id, slug, row)

    _prepare_row_for_render(row, metadata, link_target)

    return render_template("partials/analyzer_row.html", row=row)


def _build_image_data_url(image_bytes: bytes, mime_type: str) -> str:
    image_b64 = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{mime_type};base64,{image_b64}"


def _persist_source_url(phash: str | None, image_url: str | None) -> None:
    if not (image_url and phash):
        return

    registry_row = _get_or_create_registry(phash)
    record = ImageSource(image_id=registry_row.id, url=image_url)
    db.session.add(record)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()


def _maybe_auto_vote(phash: str | None, auto_vote: str | None, voter_id: str) -> None:
    if not auto_vote:
        return

    auto_vote_slug = auto_vote if auto_vote in {"real", "edited", "ai"} else None
    if not auto_vote_slug:
        return

    if not phash:
        flash("Automatic voting could not be completed for this image.")
        return

    success, status = _apply_vote(phash, auto_vote_slug, voter_id)
    if not success:
        flash("Automatic voting is temporarily unavailable. Please try again.")
        return

    if status == "updated":
        flash("Vote updated automatically via shared link.")
    elif status == "unchanged":
        flash("This vote was already recorded for you; showing latest results.")
    else:
        flash("Vote recorded automatically via shared link.")


def _attach_vote_history(
    human_row: dict | None, registry_id: int | None, voter_id: str
) -> None:
    if human_row is None or registry_id is None:
        return

    history_row = VoteHistory.query.filter_by(
        image_id=registry_id,
        voter_id=voter_id,
    ).first()

    data = human_row.get("data") or {}
    data["current_vote"] = history_row.choice if history_row else None
    human_row["data"] = data


def _build_analyzer_error_row(spec, message: str) -> dict[str, object]:
    return {
        "name": spec.name,
        "slug": spec.slug,
        "status": "ERROR",
        "summary": message,
        "data": {},
        "template": DEFAULT_ANALYZER_TEMPLATE,
    }


def _prepare_row_for_render(
    row: dict[str, object],
    metadata: dict[str, object] | None,
    link_target: str | None,
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
    }

    if row.get("slug") != "human":
        return

    registry_id = metadata.get("registry_id")
    if registry_id is None:
        return

    voter_id = _build_voter_id(_get_client_ip())
    _attach_vote_history(row, registry_id, voter_id)


def _load_cached_analyzer_row(analysis_id: str, slug: str) -> dict[str, object] | None:
    path = _analysis_row_path(analysis_id, slug)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError:
        return None


def _store_cached_analyzer_row(analysis_id: str, slug: str, row: dict[str, object]) -> None:
    path = _analysis_row_path(analysis_id, slug)
    serialized = json.dumps(row, default=str)
    path.write_text(serialized, encoding="utf-8")


def _prime_analyzer_rows(analysis_id: str, context) -> None:
    rows = run_all_analyzers(context)
    for row in rows:
        slug = row.get("slug")
        if not slug:
            continue
        _store_cached_analyzer_row(analysis_id, slug, row)


def _handle_remote_analysis(image_url: str, vote_slug: str | None, template_name: str):
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
        template_name=template_name,
    )


@bp.route("/analyze", methods=["GET", "POST"])
def analyze():
    if request.method == "GET":
        image_url = (request.args.get("url") or "").strip()
        vote_slug = (request.args.get("vote") or "").strip().lower()
        if vote_slug not in {"real", "edited", "ai"}:
            vote_slug = None

        return _handle_remote_analysis(image_url, vote_slug, "result.html")

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

    return _handle_remote_analysis(image_url, vote_slug, "result_mini.html")


@bp.route("/vote", methods=["POST"])
def vote():
    phash = (request.form.get("phash") or "").strip()
    vote_kind = (request.form.get("vote") or "").strip().lower()
    source_type = (request.form.get("source_type") or "").strip().lower()
    analysis_link = (request.form.get("analysis_link") or "").strip()

    if not phash or vote_kind not in {"real", "edited", "ai"}:
        flash("Invalid vote request.")
        return redirect(url_for("main.index"))

    voter_id = _build_voter_id(_get_client_ip())
    success, status = _apply_vote(phash, vote_kind, voter_id)
    if not success:
        flash("Voting is temporarily unavailable. Please try again.")
        return redirect(url_for("main.index"))

    flash("Thanks for your vote.")
    redirect_target = url_for("main.index")
    if source_type == "url" and analysis_link.startswith("/"):
        redirect_target = analysis_link
    return redirect(redirect_target)


def _apply_vote(phash: str, vote_kind: str, voter_id: str) -> tuple[bool, str | None]:
    if vote_kind not in {"real", "edited", "ai"}:
        return False, None

    registry_row = _get_or_create_registry(phash)
    record = _get_or_create_consensus(registry_row)

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


def _get_or_create_registry(phash: str) -> ImageRegistry:
    registry_row = ImageRegistry.query.filter_by(phash=phash).first()
    if registry_row is None:
        registry_row = ImageRegistry(phash=phash)
        db.session.add(registry_row)
        db.session.flush()
    return registry_row


def _get_or_create_consensus(registry_row: ImageRegistry) -> ImageConsensus:
    record = ImageConsensus.query.filter_by(image_id=registry_row.id).first()
    if record is None:
        record = ImageConsensus(image_id=registry_row.id)
        db.session.add(record)
    return record

from io import BytesIO
import json
import math

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
from PIL import Image, ImageOps

from . import csrf, ingestion
from .analysis_service import (
    handle_remote_analysis,
    perform_analysis,
    render_analyzer_fragment_html,
    _prepare_row_for_render,
)
from .analyzers.manager import ANALYZERS
from .analysis_cache import load_analysis_payload
from .containment_service import save_containment_link
from .config_service import (
    DONATION_GOAL_CENTS,
    get_global_config,
    increment_total_donated,
    parse_amount_to_cents,
)
from .registry import prepare_analysis_context
from .voting_service import VOTE_CHOICES, apply_vote, get_voter_id
from .analyzers.synthid import execute_synthid_search
from .analyzers.manager import get_analyzer_spec, _format_result

bp = Blueprint("main", __name__)

MIN_CROP_PIXELS = 150
MIN_CROP_ENTROPY = 1.0
MIN_CROP_ENTROPY_LOOSE = 0.5
MIN_CROP_CONTRAST = 8.0


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


@bp.route("/analysis/<analysis_id>/crop", methods=["POST"])
def crop_analysis(analysis_id: str):
    payload = load_analysis_payload(analysis_id)
    if payload is None:
        flash("Original image expired. Please re-upload to crop.")
        response = redirect(url_for("main.index"))
        response.status_code = 410
        return response

    crop_box = _parse_normalized_box(request.form)
    if crop_box is None:
        flash("Invalid crop selection. Please try again.")
        return _rerender_original(payload)

    image_bytes, metadata = payload

    try:
        cropped_bytes, sanitized_box = _crop_image_bytes(image_bytes, crop_box)
    except ValueError as exc:
        flash(str(exc))
        return _rerender_original(payload)

    child_context = prepare_analysis_context(cropped_bytes)
    parent_registry_id = metadata.get("registry_id")
    if parent_registry_id:
        try:
            save_containment_link(
                parent_registry_id, child_context.registry_id, sanitized_box
            )
        except Exception:
            # Best effort; containment links are helpful but not critical.
            current_app.logger.exception("Failed to save containment link")

    return perform_analysis(
        cropped_bytes,
        "image/png",
        "file",
        context=child_context,
        crop_box=sanitized_box,
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


@bp.route("/analysis/<analysis_id>/synthid/run", methods=["POST"])
def run_synthid(analysis_id: str):
    # 1. Load context
    payload = load_analysis_payload(analysis_id)
    if not payload:
        return "Analysis expired", 410
    image_bytes, metadata = payload

    # 2. Rehydrate Context
    context = prepare_analysis_context(image_bytes)

    # 3. Run Expensive Logic
    raw_result = execute_synthid_search(analysis_id, context)

    # 4. Format for Template
    spec = get_analyzer_spec("synthid")
    formatted_row = _format_result(spec, raw_result)

    # 5. Inject full context (links, matches, etc.)
    _prepare_row_for_render(
        formatted_row,
        metadata,
        link_target="_blank",
        analysis_id=analysis_id,
    )

    # 6. Render just the row
    return render_template("partials/analyzer_row.html", row=formatted_row)


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


def _parse_normalized_box(form) -> tuple[float, float, float, float] | None:
    fields = ("crop_left", "crop_top", "crop_width", "crop_height")
    values: list[float] = []
    for field in fields:
        raw = form.get(field)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(value):
            return None
        if value < 0.0 or value > 1.0:
            return None
        values.append(value)
    left, top, width, height = values
    if width <= 0 or height <= 0:
        return None
    if left + width > 1.0 or top + height > 1.0:
        return None
    return left, top, width, height


def _crop_image_bytes(
    image_bytes: bytes,
    normalized_box: tuple[float, float, float, float],
) -> tuple[bytes, tuple[float, float, float, float]]:
    left_norm, top_norm, width_norm, height_norm = normalized_box

    with Image.open(BytesIO(image_bytes)) as img:
        img = ImageOps.exif_transpose(img)

        img_width, img_height = img.size
        if img_width <= 0 or img_height <= 0:
            raise ValueError("Unable to crop this image.")

        left_px = int(round(left_norm * img_width))
        top_px = int(round(top_norm * img_height))
        width_px = int(round(width_norm * img_width))
        height_px = int(round(height_norm * img_height))

        left_px = max(0, min(left_px, img_width - 1))
        top_px = max(0, min(top_px, img_height - 1))
        width_px = max(1, min(width_px, img_width - left_px))
        height_px = max(1, min(height_px, img_height - top_px))

        if width_px < MIN_CROP_PIXELS or height_px < MIN_CROP_PIXELS:
            raise ValueError(f"Crop must be at least {MIN_CROP_PIXELS}px on each side.")

        right_px = left_px + width_px
        bottom_px = top_px + height_px
        cropped = img.crop((left_px, top_px, right_px, bottom_px))

        entropy, contrast = _calculate_entropy_and_contrast(cropped)
        if not (entropy >= MIN_CROP_ENTROPY_LOOSE or contrast >= MIN_CROP_CONTRAST):
            raise ValueError(
                "Crop is too uniform or low contrast. Please select a more detailed region."
            )

        buffer = BytesIO()
        cropped.save(buffer, format="PNG")
        sanitized_box = (
            left_px / img_width,
            top_px / img_height,
            width_px / img_width,
            height_px / img_height,
        )
        return buffer.getvalue(), sanitized_box


def _calculate_entropy_and_contrast(image: Image.Image) -> tuple[float, float]:
    histogram = image.convert("L").histogram()
    total = sum(histogram)
    if total == 0:
        return 0.0, 0.0

    entropy = 0.0
    mean = 0.0
    for value, count in enumerate(histogram):
        if count == 0:
            continue
        probability = count / total
        entropy -= probability * math.log2(probability)
        mean += value * probability

    variance = 0.0
    for value, count in enumerate(histogram):
        if count == 0:
            continue
        probability = count / total
        variance += probability * ((value - mean) ** 2)

    contrast = math.sqrt(variance)
    return entropy, contrast


def _rerender_original(payload):
    image_bytes, metadata = payload
    mime_type = metadata.get("mime_type", "application/octet-stream")
    source = metadata.get("source", "file")
    image_url = metadata.get("image_url")
    crop_box = metadata.get("crop_box")
    return perform_analysis(
        image_bytes,
        mime_type,
        source,
        image_url=image_url,
        crop_box=crop_box,
    )

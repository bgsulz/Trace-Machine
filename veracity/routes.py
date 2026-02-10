from io import BytesIO
import json
import math

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from PIL import Image, ImageOps

from . import csrf, ingestion, limiter
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
from .synthid_service import SYNTHID_CHOICES, apply_synthid_report
from .analyzers.tineye import call_tineye_api, process_tineye_response, get_shame_list_matchers, build_summary
from .analyzers.manager import get_analyzer_spec, _format_result
from .batch_service import process_batch_urls, MAX_BATCH_URLS
from .lookup_service import lookup_urls

bp = Blueprint("main", __name__)

EXPIRED_MESSAGE = "Analysis expired. Please submit the image again."
RATE_LIMIT_MESSAGE = "Rate limit reached (5 per hour). Please wait before trying again."


@bp.errorhandler(429)
def handle_rate_limit(e):
    """Handle rate limit exceeded errors."""
    if request.headers.get("HX-Request"):
        # Extract analysis_id from request path for retry button
        analysis_id = request.view_args.get("analysis_id") if request.view_args else None
        spec = get_analyzer_spec("tineye")
        row = {
            "name": spec.name,
            "slug": spec.slug,
            "status": "ERROR",
            "summary": RATE_LIMIT_MESSAGE,
            "data": {},
            "template": spec.template,
            "tooltip": spec.tooltip,
            "info_id": f"info-{spec.slug}",
            "context": {"analysis_id": analysis_id},
        }
        return render_template("partials/analyzer_row.html", row=row), 429
    # For regular requests, flash and redirect
    flash(RATE_LIMIT_MESSAGE)
    return redirect(url_for("main.index"))


def _expired_analysis_response():
    """Return a consistent response for expired analysis across all routes."""
    if request.headers.get("HX-Request"):
        # HTMX request: redirect via header
        flash(EXPIRED_MESSAGE)
        response = make_response("", 200)
        response.headers["HX-Redirect"] = url_for("main.index")
        return response
    # Regular request: flash + redirect with 410
    flash(EXPIRED_MESSAGE)
    response = redirect(url_for("main.index"))
    response.status_code = 410
    return response


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
    payload = load_analysis_payload(analysis_id)
    if payload is None:
        return _expired_analysis_response()
    mini = request.args.get("mini") == "1"
    link_target = "_blank" if mini else None
    refresh = request.args.get("refresh") == "1"
    return render_analyzer_fragment_html(
        analysis_id,
        slug,
        link_target=link_target,
        refresh=refresh,
        mini=mini,
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
        return _expired_analysis_response()

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


@bp.route("/analysis/<analysis_id>/tineye/run", methods=["POST"])
@limiter.limit("5 per hour")  # per-IP
@limiter.limit("100 per hour", key_func=lambda: "global")  # global across all users
def run_tineye(analysis_id: str):
    payload = load_analysis_payload(analysis_id)
    if not payload:
        return _expired_analysis_response()
    image_bytes, metadata = payload

    # Generate external URL for the image
    image_url = url_for(
        "main.serve_analysis_image", analysis_id=analysis_id, _external=True
    )

    # Call TinEye API directly without persisting results
    api_result = call_tineye_api(image_url=image_url)

    if not api_result["success"]:
        # Log the actual error for debugging, but show a clearer message to users
        error_msg = api_result.get("error", "")
        if error_msg:
            current_app.logger.warning("TinEye API error: %s", error_msg)

        # Check if it looks like a rate limit error
        error_lower = error_msg.lower()
        if "429" in error_msg or "rate" in error_lower or "too many" in error_lower:
            summary = "TinEye rate limit reached. Please wait a few minutes before trying again."
        elif "key" in error_lower or "auth" in error_lower:
            summary = "TinEye API configuration error. Please contact the site administrator."
        else:
            summary = "Unable to complete TinEye search. Please try again later."

        raw_result = {
            "status": "ERROR",
            "summary": summary,
            "data": {
                "allow_manual_refresh": False,
            },
        }
    else:
        processed = process_tineye_response(api_result, matchers=get_shame_list_matchers())

        summary = build_summary(
            processed["total_matches"],
            processed["filtered_match_count"],
            processed["earliest_date"],
            processed["on_shame_list"],
        )

        raw_result = {
            "status": "FOUND" if processed["filtered_match_count"] > 0 else "NOT FOUND",
            "summary": summary,
            "data": {
                "total_matches": processed["total_matches"],
                "earliest_date": processed["earliest_date"],
                "on_shame_list": processed["on_shame_list"],
                "buckets": processed["buckets"],
                "allow_manual_refresh": True,
            },
        }

    spec = get_analyzer_spec("tineye")
    formatted_row = _format_result(spec, raw_result)

    _prepare_row_for_render(
        formatted_row,
        metadata,
        link_target="_blank",
        analysis_id=analysis_id,
    )

    return render_template("partials/analyzer_row.html", row=formatted_row)


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
    if request.headers.get("HX-Request") and analysis_id:
        payload = load_analysis_payload(analysis_id)
        if payload is None:
            return _expired_analysis_response()
        html = render_analyzer_fragment_html(
            analysis_id,
            "human",
            link_target=link_target or None,
            refresh=True,
            mini=mini,
        )
        response = make_response(html)
        response.headers["HX-Trigger"] = json.dumps({"showToast": "Thanks for your vote."})
        return response
    flash("Thanks for your vote.")
    return redirect(redirect_target)


@bp.route("/synthid-report", methods=["POST"])
def synthid_report():
    report = (request.form.get("report") or "").strip().lower()
    analysis_id = (request.form.get("analysis_id") or "").strip()

    if not analysis_id:
        if request.headers.get("HX-Request"):
            return _expired_analysis_response()
        flash("Invalid report request.")
        return redirect(url_for("main.index"))

    payload = load_analysis_payload(analysis_id)
    if payload is None:
        return _expired_analysis_response()
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

    if request.headers.get("HX-Request") and analysis_id:
        html = render_analyzer_fragment_html(
            analysis_id,
            "synthid",
            link_target=None,
            refresh=True,
        )
        response = make_response(html)
        msg = "SynthID report recorded." if status == "recorded" else "SynthID report updated."
        if status == "unchanged":
            msg = "You already submitted this report."
        response.headers["HX-Trigger"] = json.dumps({"showToast": msg})
        return response

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


@bp.route("/batch")
def batch():
    return render_template("batch.html")


@bp.route("/batch", methods=["POST"])
@limiter.limit("3/minute")
def batch_submit():
    raw_text = (request.form.get("urls") or "").strip()
    if not raw_text:
        flash("Please paste at least one image URL.")
        return redirect(url_for("main.batch"))

    urls = [line.strip() for line in raw_text.splitlines() if line.strip()]
    if not urls:
        flash("Please paste at least one image URL.")
        return redirect(url_for("main.batch"))

    if len(urls) > MAX_BATCH_URLS:
        flash(f"Maximum {MAX_BATCH_URLS} URLs per batch.")
        return redirect(url_for("main.batch"))

    # Validate each URL format
    valid_urls = []
    results = []
    for url in urls:
        if not url.startswith(("http://", "https://")):
            results.append({
                "url": url,
                "analysis_id": None,
                "error": "Invalid URL format",
                "image_data_url": None,
                "public_url_display": url[:60],
            })
        else:
            valid_urls.append(url)

    if valid_urls:
        batch_results = process_batch_urls(valid_urls)
        # Merge: place batch results in order after validation errors
        valid_iter = iter(batch_results)
        merged = []
        valid_set = set(valid_urls)
        for url in urls:
            if url in valid_set:
                merged.append(next(valid_iter))
                valid_set.discard(url)
            else:
                # Find the matching error result
                for r in results:
                    if r["url"] == url:
                        merged.append(r)
                        break
        results = merged

    return render_template("batch_results.html", results=results)


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


MAX_LOOKUP_URLS = 50


@bp.route("/api/lookup", methods=["POST"])
@csrf.exempt
@limiter.limit("30/minute")
@limiter.limit("500/hour", key_func=lambda: "global")
def api_lookup():
    data = request.get_json(silent=True)
    if not data or not isinstance(data.get("urls"), list):
        return jsonify({"error": "Request body must be JSON with a 'urls' array."}), 400

    urls = data["urls"]
    if len(urls) > MAX_LOOKUP_URLS:
        return jsonify({"error": f"Maximum {MAX_LOOKUP_URLS} URLs per request."}), 400

    # Filter to strings only
    urls = [u for u in urls if isinstance(u, str) and u]

    results = lookup_urls(urls)
    return jsonify({"results": results})


@bp.route("/dev/mini-test")
def dev_mini_test():
    """Dev-only page to test the analyze-mini iframe view."""
    if not current_app.debug:
        abort(404)
    return render_template("dev_mini_test.html")

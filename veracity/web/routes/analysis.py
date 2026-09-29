from __future__ import annotations

import json
import math
from collections.abc import Callable
from io import BytesIO

from flask import Blueprint, abort, current_app, flash, make_response, redirect, render_template, request, send_file, url_for
from PIL import Image, ImageOps

from ... import ingestion
from ...analysis_cache import load_analysis_payload
from ...services.analysis_service import (
    _prepare_row_for_render,
    handle_remote_analysis,
    perform_analysis,
    render_analysis_page,
    render_analyzer_fragment_html,
)
from ...analyzers.manager import _format_result, get_analyzer_spec
from ...autocrop import detect_overlay_crop
from ...services.containment_service import save_containment_link
from ...services.remote_image_service import fetch_remote_image
from ...registry import prepare_analysis_context
from ...services.reporting import build_report_payload
from ...services.voting_service import VOTE_CHOICES

MIN_CROP_PIXELS = 150
MIN_CROP_ENTROPY_LOOSE = 0.5
MIN_CROP_CONTRAST = 8.0


def register_analysis_routes(
    bp: Blueprint,
    expired_analysis_response: Callable[[], object],
) -> None:
    @bp.route("/analysis/<analysis_id>")
    def view_analysis(analysis_id: str):
        page = render_analysis_page(analysis_id)
        if page is None:
            return expired_analysis_response()
        return page

    @bp.route("/analysis/<analysis_id>/analyzers/<slug>")
    def analyzer_fragment(analysis_id: str, slug: str):
        payload = load_analysis_payload(analysis_id)
        if payload is None:
            return expired_analysis_response()
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
        """Serve raw image bytes for a given analysis ID."""
        payload = load_analysis_payload(analysis_id)
        if payload is None:
            abort(404)

        image_bytes, metadata = payload
        mime_type = metadata.get("mime_type", "application/octet-stream")
        return send_file(BytesIO(image_bytes), mimetype=mime_type, max_age=3600)

    @bp.route("/analysis/<analysis_id>/export.json")
    def export_analysis_json(analysis_id: str):
        try:
            report = build_report_payload(analysis_id)
        except KeyError:
            return expired_analysis_response()

        body = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=False)
        response = make_response(body, 200)
        response.mimetype = "application/json"
        return response

    @bp.route("/analysis/<analysis_id>/export.html")
    def export_analysis_html(analysis_id: str):
        try:
            report = build_report_payload(analysis_id)
        except KeyError:
            return expired_analysis_response()

        return render_template("export_report.html", report=report)

    @bp.route("/analysis/<analysis_id>/crop", methods=["POST"])
    def crop_analysis(analysis_id: str):
        payload = load_analysis_payload(analysis_id)
        if payload is None:
            return expired_analysis_response()

        crop_box = _parse_normalized_box(request.form)
        if crop_box is None:
            flash("Invalid crop selection. Please try again.")
            return redirect(url_for("main.view_analysis", analysis_id=analysis_id))

        image_bytes, metadata = payload

        try:
            cropped_bytes, sanitized_box = _crop_image_bytes(image_bytes, crop_box)
        except ValueError as exc:
            flash(str(exc))
            return redirect(url_for("main.view_analysis", analysis_id=analysis_id))

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

    @bp.route("/analysis/<analysis_id>/autocrop", methods=["POST"])
    def autocrop_analysis(analysis_id: str):
        payload = load_analysis_payload(analysis_id)
        if payload is None:
            return expired_analysis_response()

        image_bytes, metadata = payload

        result = detect_overlay_crop(image_bytes)
        if not result.has_overlay or result.crop_box is None:
            flash("Auto-crop could not find a clear overlay to remove.")
            return redirect(url_for("main.view_analysis", analysis_id=analysis_id))

        try:
            cropped_bytes, sanitized_box = _crop_image_bytes(image_bytes, result.crop_box)
        except ValueError as exc:
            flash(str(exc))
            return redirect(url_for("main.view_analysis", analysis_id=analysis_id))

        child_context = prepare_analysis_context(cropped_bytes)
        parent_registry_id = metadata.get("registry_id")
        if parent_registry_id:
            try:
                save_containment_link(
                    parent_registry_id, child_context.registry_id, sanitized_box
                )
            except Exception:
                current_app.logger.exception("Failed to save auto-crop containment link")

        return perform_analysis(
            cropped_bytes,
            "image/png",
            "file",
            context=child_context,
            crop_box=sanitized_box,
        )

    @bp.route("/analyze/matched-region", methods=["POST"])
    def analyze_matched_region():
        image_url = (request.form.get("image_url") or "").strip()
        if not image_url:
            flash("Matched source URL is missing.")
            return redirect(url_for("main.index"))

        crop_box = _parse_normalized_box(request.form)
        if crop_box is None:
            flash("Matched region is unavailable. Please re-open the source image.")
            return redirect(url_for("main.analyze", url=image_url))

        try:
            fetched = fetch_remote_image(image_url)
        except ingestion.IngestionError as exc:
            flash(str(exc))
            return redirect(url_for("main.analyze", url=image_url))

        try:
            cropped_bytes, sanitized_box = _crop_image_bytes(
                fetched.image_bytes,
                crop_box,
            )
        except ValueError as exc:
            flash(str(exc))
            return redirect(url_for("main.analyze", url=image_url))

        child_context = prepare_analysis_context(cropped_bytes)
        parent_registry_id = _parse_parent_registry_id(
            request.form.get("parent_registry_id")
        )
        if parent_registry_id is not None:
            try:
                save_containment_link(
                    parent_registry_id,
                    child_context.registry_id,
                    sanitized_box,
                )
            except Exception:
                current_app.logger.exception("Failed to save matched-region containment link")

        return perform_analysis(
            cropped_bytes,
            "image/png",
            "url",
            image_url=fetched.fetch_url,
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

        file = request.files.get("file")
        image_url = (request.form.get("image_url") or "").strip()
        if (not file or not file.filename) and image_url:
            return redirect(url_for("main.analyze", url=image_url))

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


def _parse_parent_registry_id(raw_value: str | None) -> int | None:
    try:
        value = int(raw_value or "")
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


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

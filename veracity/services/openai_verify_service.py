"""Automated checks with OpenAI's content provenance API.

OpenAI's Verify tool has an API (``POST /v1/content_provenance_checks``) that
checks an image for OpenAI's own signals: its C2PA manifest and the SynthID
watermark OpenAI licenses from Google. It only recognizes OpenAI-made content.

Checks are on demand (a visitor clicks), because the image is sent to OpenAI,
the endpoint has strict rate limits, and pricing isn't published. Results are
stored as provenance facts on the image, so near-identical copies can show
them without another call. The feature is hidden unless OPENAI_API_KEY is set.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from io import BytesIO
from typing import Any

import requests
from flask import current_app
from sqlalchemy.exc import IntegrityError

from .. import db
from ..models import ProvenanceFact
from .preview_service import PreviewError, render_display_jpeg

logger = logging.getLogger(__name__)

API_URL = "https://api.openai.com/v1/content_provenance_checks"
FACT_ANALYZER = "openai_verify"
SUPPORTED_MIME_TYPES = {"image/png", "image/jpeg", "image/webp"}
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
REQUEST_TIMEOUT = 60  # seconds


class OpenAIVerifyError(Exception):
    def __init__(self, message: str, *, retry_after: int | None = None):
        super().__init__(message)
        self.retry_after = retry_after


def api_key() -> str:
    return (current_app.config.get("OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY") or "").strip()


def is_configured() -> bool:
    return bool(api_key())


def run_check(image_bytes: bytes, mime_type: str) -> dict[str, Any]:
    """Send the image to OpenAI and return a normalized result."""
    key = api_key()
    if not key:
        raise OpenAIVerifyError("Automated OpenAI checks aren't set up on this server.")

    converted = mime_type not in SUPPORTED_MIME_TYPES
    upload, upload_type = image_bytes, mime_type
    if converted:
        # e.g. HEIC/TIFF: OpenAI only takes PNG/JPEG/WebP. A converted copy
        # loses its C2PA manifest but keeps pixel watermarks like SynthID.
        try:
            upload, upload_type = render_display_jpeg(image_bytes), "image/jpeg"
        except PreviewError as exc:
            raise OpenAIVerifyError("This image couldn't be prepared for OpenAI's checker.") from exc
    if len(upload) > MAX_UPLOAD_BYTES:
        raise OpenAIVerifyError("This image is larger than OpenAI's 50 MB limit.")

    extension = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}[upload_type]
    try:
        response = requests.post(
            API_URL,
            headers={"Authorization": f"Bearer {key}"},
            files={"file": (f"image.{extension}", BytesIO(upload), upload_type)},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise OpenAIVerifyError("Couldn't reach OpenAI's checker. Please try again later.") from exc

    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After", "")
        raise OpenAIVerifyError(
            "OpenAI's checker is rate-limited right now. Please try again in a few minutes.",
            retry_after=int(retry_after) if retry_after.isdigit() else None,
        )
    if response.status_code == 400:
        raise OpenAIVerifyError("OpenAI's checker couldn't process this image.")
    if response.status_code != 200:
        logger.warning("OpenAI provenance check failed: HTTP %s", response.status_code)
        raise OpenAIVerifyError("OpenAI's checker isn't available right now.")

    try:
        payload = response.json()
    except ValueError as exc:
        raise OpenAIVerifyError("OpenAI's checker returned an unexpected response.") from exc
    return normalize_response(payload, converted=converted)


def normalize_response(payload: dict[str, Any], *, converted: bool = False) -> dict[str, Any]:
    """Reduce the API response to what we store and display."""
    signals: dict[str, dict[str, Any]] = {}
    for item in payload.get("results") or []:
        kind = item.get("type")
        if kind not in ("c2pa", "synthid"):
            continue
        signals[kind] = {
            "detected": item.get("outcome") == "detected",
            "validation_state": item.get("validation_state"),
            "issuer": item.get("issuer"),
            "model": item.get("model"),
            "generated_at": item.get("generated_at"),
        }
    created = payload.get("created_at")
    checked_at = (
        datetime.fromtimestamp(created, tz=timezone.utc)
        if isinstance(created, (int, float))
        else datetime.now(timezone.utc)
    )
    return {
        "checked_at": checked_at.isoformat(timespec="seconds"),
        "converted": converted,
        "c2pa": signals.get("c2pa"),
        "synthid": signals.get("synthid"),
        "detected": any(signal["detected"] for signal in signals.values()),
    }


def record(registry_id: int, result: dict[str, Any]) -> None:
    fact = ProvenanceFact(
        image_id=registry_id,
        analyzer=FACT_ANALYZER,
        data=json.dumps(result, sort_keys=True),
    )
    try:
        db.session.add(fact)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()


def results_from_facts(facts) -> list[dict[str, Any]]:
    """Parse stored results, newest first."""
    parsed = []
    for fact in facts or []:
        if getattr(fact, "analyzer", None) != FACT_ANALYZER:
            continue
        try:
            result = json.loads(fact.data)
        except (TypeError, ValueError):
            continue
        if isinstance(result, dict):
            parsed.append(result)
    # ISO-8601 UTC timestamps sort chronologically as strings.
    return sorted(parsed, key=lambda r: str(r.get("checked_at", "")), reverse=True)

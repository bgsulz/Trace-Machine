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
import threading
from datetime import datetime, timedelta, timezone
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
# (connect, read) seconds; keep the total under gunicorn's default 30 s timeout.
REQUEST_TIMEOUT = (5, 25)
# Checks tie up a web worker while OpenAI responds; cap them per process.
_CONCURRENT_CHECKS = threading.BoundedSemaphore(2)
# Reuse a stored result this recent instead of paying for another call.
REUSE_WINDOW = timedelta(days=7)
# Only a manifest OpenAI validated counts as OpenAI Content Credentials.
_VALID_C2PA_STATES = {"trusted", "valid"}


class OpenAIVerifyError(Exception):
    def __init__(self, message: str, *, retry_after: int | None = None):
        super().__init__(message)
        self.retry_after = retry_after


_OWNED_SESSION_KEY = "my_analyses"
_OWNED_LIMIT = 50


def remember_owner(analysis_id: str) -> None:
    """Note that this browser session created *analysis_id*."""
    from flask import session

    owned = [a for a in session.get(_OWNED_SESSION_KEY, []) if a != analysis_id]
    session[_OWNED_SESSION_KEY] = (owned + [analysis_id])[-_OWNED_LIMIT:]


def may_check(analysis_id: str, metadata: dict[str, Any]) -> bool:
    """Whether this visitor may send the image to OpenAI.

    Images analyzed from a public URL are public already. Uploaded images
    can only be sent by the session that uploaded them, so sharing a
    permalink doesn't let others forward someone's private upload.
    """
    from flask import session

    if not is_configured():
        return False
    if metadata.get("source") == "url" and metadata.get("public_url"):
        return True
    return analysis_id in session.get(_OWNED_SESSION_KEY, [])


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
    if not _CONCURRENT_CHECKS.acquire(blocking=False):
        raise OpenAIVerifyError("Other automated checks are running. Please try again in a moment.")
    try:
        response = requests.post(
            API_URL,
            headers={"Authorization": f"Bearer {key}"},
            files={"file": (f"image.{extension}", BytesIO(upload), upload_type)},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise OpenAIVerifyError("Couldn't reach OpenAI's checker. Please try again later.") from exc
    finally:
        _CONCURRENT_CHECKS.release()

    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After", "")
        seconds = int(retry_after) if retry_after.isdigit() else None
        wait = f"in about {max(1, round(seconds / 60))} min" if seconds else "in a few minutes"
        raise OpenAIVerifyError(
            f"OpenAI's checker is rate-limited right now. Please try again {wait}.",
            retry_after=seconds,
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


def normalize_response(payload: Any, *, converted: bool = False) -> dict[str, Any]:
    """Reduce the API response to what we store and display.

    A C2PA manifest only counts as detected when OpenAI validated it; an
    invalid one (e.g. forged or tampered) is kept for display but doesn't
    count. Unexpected shapes are ignored rather than trusted.
    """
    signals: dict[str, dict[str, Any]] = {}
    items = payload.get("results") if isinstance(payload, dict) else None
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or item.get("type") not in ("c2pa", "synthid"):
            continue
        kind = item["type"]
        state = item.get("validation_state")
        outcome_detected = item.get("outcome") == "detected"
        detected = outcome_detected and (kind != "c2pa" or state in _VALID_C2PA_STATES)
        previous = signals.get(kind)
        if previous and previous["detected"] and not detected:
            continue  # duplicates: a detection wins
        signals[kind] = {
            "detected": detected,
            "present_but_invalid": kind == "c2pa" and outcome_detected and not detected,
            "validation_state": state if isinstance(state, str) else None,
            "issuer": item.get("issuer") if isinstance(item.get("issuer"), str) else None,
            "model": item.get("model") if isinstance(item.get("model"), str) else None,
            "generated_at": item.get("generated_at") if isinstance(item.get("generated_at"), str) else None,
        }

    checked_at = datetime.now(timezone.utc)
    created = payload.get("created_at") if isinstance(payload, dict) else None
    if isinstance(created, (int, float)) and not isinstance(created, bool):
        try:
            checked_at = datetime.fromtimestamp(created, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            pass
    return {
        "checked_at": checked_at.isoformat(timespec="seconds"),
        "converted": converted,
        "c2pa": signals.get("c2pa"),
        "synthid": signals.get("synthid"),
        "detected": any(signal["detected"] for signal in signals.values()),
    }


def recent_result(facts, *, converted: bool) -> dict[str, Any] | None:
    """A stored result for this image recent enough to reuse, if any."""
    cutoff = datetime.now(timezone.utc) - REUSE_WINDOW
    for result in results_from_facts(facts):
        try:
            checked = datetime.fromisoformat(str(result.get("checked_at")))
        except ValueError:
            continue
        # A converted-copy check can't stand in for a full one.
        if checked >= cutoff and (result.get("converted") or False) <= converted:
            return result
    return None


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

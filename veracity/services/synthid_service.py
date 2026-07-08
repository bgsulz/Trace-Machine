from sqlalchemy.exc import IntegrityError

from .. import db
from ..models import ImageRegistry, SynthIDReport


GOOGLE_POSITIVE = "google_positive"
OPENAI_POSITIVE = "openai_positive"
NEGATIVE = "negative"

SYNTHID_CHOICES = {GOOGLE_POSITIVE, OPENAI_POSITIVE, NEGATIVE}
PORTAL_RESULTS = {
    GOOGLE_POSITIVE: {
        "provider": "google",
        "label": "Google Positive",
        "short_label": "Google",
        "check_label": "Check Google",
    },
    OPENAI_POSITIVE: {
        "provider": "openai",
        "label": "OpenAI Positive",
        "short_label": "OpenAI",
        "check_label": "OpenAI Verify",
    },
    NEGATIVE: {
        "provider": "portal",
        "label": "Negative",
        "short_label": "Negative",
        "check_label": "",
    },
}

# Compatibility for old callers/templates during the UI transition.
SYNTHID_DETECTORS = {
    "google_about_this_image": {
        "provider": "google",
        "label": "Google About this image",
        "short_label": "Google",
        "check_label": "Check Google",
    },
    "openai_verify": {
        "provider": "openai",
        "label": "OpenAI Verify",
        "short_label": "OpenAI",
        "check_label": "OpenAI Verify",
    },
}


def apply_synthid_report(
    phash: str,
    result: str,
    voter_id: str,
    provider: str = "google",
    detector: str = "google_about_this_image",
) -> tuple[bool, str | None]:
    normalized = normalize_portal_result(result, provider=provider, detector=detector)
    if normalized is None:
        return False, None

    registry_row = ImageRegistry.query.filter_by(phash=phash).first()
    if registry_row is None:
        return False, None

    report = SynthIDReport.query.filter_by(
        image_id=registry_row.id,
        voter_id=voter_id,
    ).first()

    status = "unchanged"
    if report is None:
        report = SynthIDReport(
            image_id=registry_row.id,
            voter_id=voter_id,
            result=normalized,
        )
        db.session.add(report)
        status = "recorded"
    elif report.result != normalized:
        report.result = normalized
        status = "updated"

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()

        report = SynthIDReport.query.filter_by(
            image_id=registry_row.id,
            voter_id=voter_id,
        ).first()
        if report is None:
            return False, None

        if report.result == normalized:
            return True, "unchanged"

        report.result = normalized
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            return False, None
        return True, "updated"

    return True, status


def normalize_portal_result(
    result: str,
    *,
    provider: str = "",
    detector: str = "",
) -> str | None:
    result = (result or "").strip().lower()
    provider = (provider or "").strip().lower()
    detector = (detector or "").strip().lower()

    if result in SYNTHID_CHOICES:
        return result
    if result == "not_detected":
        return NEGATIVE
    if result != "detected":
        return None

    if provider == "openai" or detector == "openai_verify":
        return OPENAI_POSITIVE
    if provider in {"", "google"} or detector in {"", "google_about_this_image"}:
        return GOOGLE_POSITIVE
    return None


def portal_counts_from_reports(reports) -> dict[str, int]:
    counts = {GOOGLE_POSITIVE: 0, OPENAI_POSITIVE: 0, NEGATIVE: 0}
    for report in reports or []:
        result = normalize_portal_result(str(getattr(report, "result", "") or ""))
        if result is not None:
            counts[result] += 1
    return counts


def portal_verdict(counts: dict[str, int]) -> str | None:
    google = int(counts.get(GOOGLE_POSITIVE) or 0)
    openai = int(counts.get(OPENAI_POSITIVE) or 0)
    negative = int(counts.get(NEGATIVE) or 0)

    if google > 0 and openai > 0:
        return "contested"
    if google > negative:
        return GOOGLE_POSITIVE
    if openai > negative:
        return OPENAI_POSITIVE
    if negative > google + openai:
        return NEGATIVE
    if negative > 0 and google == 0 and openai == 0:
        return NEGATIVE
    return None


def portal_payload_from_counts(counts: dict[str, int]) -> dict[str, object]:
    payload = {
        GOOGLE_POSITIVE: int(counts.get(GOOGLE_POSITIVE) or 0),
        OPENAI_POSITIVE: int(counts.get(OPENAI_POSITIVE) or 0),
        NEGATIVE: int(counts.get(NEGATIVE) or 0),
    }
    verdict = portal_verdict(payload)
    payload["verdict"] = verdict
    payload["contested"] = verdict == "contested"
    return payload

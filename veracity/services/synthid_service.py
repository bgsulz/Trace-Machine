from sqlalchemy.exc import IntegrityError

from .. import db
from ..models import ImageRegistry, SynthIDReport


GOOGLE_POSITIVE = "google_positive"
OPENAI_POSITIVE = "openai_positive"
META_POSITIVE = "meta_positive"
NEGATIVE = "negative"

SYNTHID_CHOICES = {GOOGLE_POSITIVE, OPENAI_POSITIVE, META_POSITIVE, NEGATIVE}
# Each provider-positive result doubles as the description of that provider's
# public checker ("portal"). Result keys are stored in the database; labels,
# URLs, and hints are display-only and safe to change.
PORTAL_RESULTS = {
    GOOGLE_POSITIVE: {
        "provider": "google",
        "label": "Google Positive",
        "short_label": "Google",
        "check_label": "Open Gemini",
        "tool": "Gemini",
        "url": "https://gemini.google.com/app",
        "hint": "Sign in, upload the image, and ask whether it was made with Google AI. Gemini checks for SynthID.",
    },
    OPENAI_POSITIVE: {
        "provider": "openai",
        "label": "OpenAI Positive",
        "short_label": "OpenAI",
        "check_label": "OpenAI Verify",
        "tool": "Verify",
        "url": "https://openai.com/verify",
        "hint": "Upload the image. Verify checks for OpenAI's C2PA manifest and SynthID watermark.",
    },
    META_POSITIVE: {
        "provider": "meta",
        "label": "Meta Positive",
        "short_label": "Meta",
        "check_label": "Meta Identify",
        "tool": "Identify",
        "url": "https://meta.ai/identification",
        "hint": "Upload the image and look for a Content Seal watermark match.",
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
        "label": "Google Gemini",
        "short_label": "Google",
        "check_label": "Open Gemini",
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
    if provider == "meta" or detector == "meta_identification":
        return META_POSITIVE
    if provider in {"", "google"} or detector in {"", "google_about_this_image"}:
        return GOOGLE_POSITIVE
    return None


def portal_counts_from_reports(reports) -> dict[str, int]:
    counts = {
        GOOGLE_POSITIVE: 0,
        OPENAI_POSITIVE: 0,
        META_POSITIVE: 0,
        NEGATIVE: 0,
    }
    for report in reports or []:
        result = normalize_portal_result(str(getattr(report, "result", "") or ""))
        if result is not None:
            counts[result] += 1
    return counts


def portal_verdict(counts: dict[str, int]) -> str | None:
    google = int(counts.get(GOOGLE_POSITIVE) or 0)
    openai = int(counts.get(OPENAI_POSITIVE) or 0)
    meta = int(counts.get(META_POSITIVE) or 0)
    negative = int(counts.get(NEGATIVE) or 0)

    positives = {
        GOOGLE_POSITIVE: google,
        OPENAI_POSITIVE: openai,
        META_POSITIVE: meta,
    }
    present = [result for result, count in positives.items() if count > 0]
    if len(present) > 1:
        return "contested"
    if present and positives[present[0]] > negative:
        return present[0]
    if negative > sum(positives.values()):
        return NEGATIVE
    if negative > 0 and not present:
        return NEGATIVE
    return None


def portal_payload_from_counts(counts: dict[str, int]) -> dict[str, object]:
    payload = {
        GOOGLE_POSITIVE: int(counts.get(GOOGLE_POSITIVE) or 0),
        OPENAI_POSITIVE: int(counts.get(OPENAI_POSITIVE) or 0),
        META_POSITIVE: int(counts.get(META_POSITIVE) or 0),
        NEGATIVE: int(counts.get(NEGATIVE) or 0),
    }
    verdict = portal_verdict(payload)
    payload["verdict"] = verdict
    payload["contested"] = verdict == "contested"
    return payload

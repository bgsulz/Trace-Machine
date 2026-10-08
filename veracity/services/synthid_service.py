from sqlalchemy.exc import IntegrityError

from .. import db
from ..models import ImageRegistry, SynthIDReport


GOOGLE_POSITIVE = "google_positive"
OPENAI_POSITIVE = "openai_positive"
META_POSITIVE = "meta_positive"
NEGATIVE = "negative"

SYNTHID_CHOICES = {GOOGLE_POSITIVE, OPENAI_POSITIVE, META_POSITIVE, NEGATIVE}
# A positive result names the provider that made the image, whichever portal
# reported it. Result keys are stored in the database; labels are display-only
# and safe to change.
PORTAL_RESULTS = {
    GOOGLE_POSITIVE: {
        "provider": "google",
        "label": "Google Positive",
        "short_label": "Google",
    },
    OPENAI_POSITIVE: {
        "provider": "openai",
        "label": "OpenAI Positive",
        "short_label": "OpenAI",
    },
    META_POSITIVE: {
        "provider": "meta",
        "label": "Meta Positive",
        "short_label": "Meta",
    },
    NEGATIVE: {
        "provider": "portal",
        "label": "Negative",
        "short_label": "Negative",
    },
}

# Public checkers ("portals") in the order people should try them, each with
# the results it can report. Fallback portals sit behind a disclosure.
PORTALS = (
    {
        "provider": "google",
        "name": "Google SynthID Detector",
        "short_name": "SynthID Detector",
        "url": "https://synthid.com/",
        "hint": "Names the maker of SynthID images from Google and partners like OpenAI.",
        "access": "Sign in with a Google, Apple, or ChatGPT account.",
        "reports": (
            {"result": GOOGLE_POSITIVE, "label": "Says Google"},
            {"result": OPENAI_POSITIVE, "label": "Says OpenAI"},
        ),
    },
    {
        "provider": "meta",
        "name": "Meta Identify",
        "short_name": "Meta Identify",
        "url": "https://meta.ai/identification",
        "hint": "Checks for Meta's Content Seal watermark, which the SynthID Detector can't read.",
        "reports": ({"result": META_POSITIVE, "label": "Positive"},),
    },
    {
        "provider": "openai",
        "name": "OpenAI Verify",
        "short_name": "OpenAI Verify",
        "url": "https://openai.com/verify",
        "hint": (
            "Checks OpenAI images for Content Credentials and SynthID. Useful if the "
            "SynthID Detector is rate-limited or you'd rather not sign in."
        ),
        "fallback": True,
        "reports": ({"result": OPENAI_POSITIVE, "label": "Positive"},),
    },
)

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

from sqlalchemy.exc import IntegrityError

from .. import db
from ..models import ImageRegistry, SynthIDReport


SYNTHID_CHOICES = {"detected", "not_detected"}
SYNTHID_SOURCE_KIND = "manual_user_report"
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
    provider, detector = _normalize_detector(provider, detector)
    if result not in SYNTHID_CHOICES or not provider or not detector:
        return False, None

    registry_row = ImageRegistry.query.filter_by(phash=phash).first()
    if registry_row is None:
        return False, None

    report = SynthIDReport.query.filter_by(
        image_id=registry_row.id,
        voter_id=voter_id,
        provider=provider,
        detector=detector,
    ).first()

    status = "unchanged"
    if report is None:
        report = SynthIDReport(
            image_id=registry_row.id,
            voter_id=voter_id,
            provider=provider,
            detector=detector,
            source_kind=SYNTHID_SOURCE_KIND,
            result=result,
        )
        db.session.add(report)
        status = "recorded"
    else:
        if report.result != result:
            report.result = result
            status = "updated"

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()

        # Likely a concurrent insert/update hit the unique constraint.
        # Recover by re-querying and returning an appropriate status.
        report = SynthIDReport.query.filter_by(
            image_id=registry_row.id,
            voter_id=voter_id,
            provider=provider,
            detector=detector,
        ).first()
        if report is None:
            return False, None

        if report.result == result:
            return True, "unchanged"

        report.result = result
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            return False, None
        return True, "updated"

    return True, status


def _normalize_detector(provider: str, detector: str) -> tuple[str | None, str | None]:
    provider = (provider or "").strip().lower()
    detector = (detector or "").strip().lower()
    spec = SYNTHID_DETECTORS.get(detector)
    if spec is None:
        return None, None
    expected_provider = str(spec["provider"])
    if provider and provider != expected_provider:
        return None, None
    return expected_provider, detector

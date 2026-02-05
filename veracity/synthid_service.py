from sqlalchemy.exc import IntegrityError

from . import db
from .models import ImageRegistry, SynthIDReport


SYNTHID_CHOICES = {"detected", "not_detected"}


def apply_synthid_report(
    phash: str, result: str, voter_id: str
) -> tuple[bool, str | None]:
    if result not in SYNTHID_CHOICES:
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
        return False, None

    return True, status

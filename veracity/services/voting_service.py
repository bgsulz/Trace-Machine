import hashlib

from flask import current_app, request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from .. import db
from ..models import ImageConsensus, ImageRegistry, ImageSource, VoteHistory


VOTE_CHOICES = {"real", "edited", "ai"}


def apply_vote(phash: str, vote_kind: str, voter_id: str) -> tuple[bool, str | None]:
    if vote_kind not in VOTE_CHOICES:
        return False, None

    # Single query: fetch registry + consensus via eager loading
    registry_row = ImageRegistry.query.options(
        joinedload(ImageRegistry.consensus)
    ).filter_by(phash=phash).first()

    if registry_row is None:
        # Image must exist before voting (created during analysis)
        return False, None

    record = registry_row.consensus
    if record is None:
        record = ImageConsensus(image_id=registry_row.id)
        db.session.add(record)

    history_row = VoteHistory.query.filter_by(
        image_id=registry_row.id,
        voter_id=voter_id,
    ).first()

    status = "unchanged"
    if history_row is None:
        history_row = VoteHistory(
            image_id=registry_row.id,
            voter_id=voter_id,
            choice=vote_kind,
        )
        db.session.add(history_row)
        _increment_vote_counts(record, vote_kind)
        status = "recorded"
    else:
        previous_choice = history_row.choice
        if previous_choice != vote_kind:
            _decrement_vote_counts(record, previous_choice)
            _increment_vote_counts(record, vote_kind)
            history_row.choice = vote_kind
            status = "updated"

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return False, None

    return True, status


def persist_source_url(phash: str | None, image_url: str | None) -> None:
    if not (image_url and phash):
        return

    registry_row = ImageRegistry.query.filter_by(phash=phash).first()
    if registry_row is None:
        return  # Image must exist (created during analysis)

    record = ImageSource(image_id=registry_row.id, url=image_url)
    db.session.add(record)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()


def get_voter_id() -> str:
    return build_voter_id(get_client_ip())


def get_client_ip() -> str:
    return request.remote_addr or "unknown"


def build_voter_id(ip_address: str) -> str:
    secret = current_app.config.get("SECRET_KEY", "")
    payload = f"{ip_address}:{secret}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _increment_vote_counts(record: ImageConsensus, vote_kind: str) -> None:
    if vote_kind == "real":
        record.vote_real = (record.vote_real or 0) + 1
    elif vote_kind == "edited":
        record.vote_edited = (record.vote_edited or 0) + 1
    else:
        record.vote_ai = (record.vote_ai or 0) + 1


def _decrement_vote_counts(record: ImageConsensus, vote_kind: str) -> None:
    if vote_kind == "real":
        record.vote_real = max((record.vote_real or 0) - 1, 0)
    elif vote_kind == "edited":
        record.vote_edited = max((record.vote_edited or 0) - 1, 0)
    else:
        record.vote_ai = max((record.vote_ai or 0) - 1, 0)

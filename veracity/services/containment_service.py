from __future__ import annotations

import json
from typing import Any, Sequence

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from .. import db
from ..models import ImageContainment, ImageRegistry


_MAX_CONTAINMENT_DEPTH = 3


def _normalize_crop_box(crop_box: Sequence[float]) -> list[float]:
    """Clamp crop box values to [0, 1] and validate length."""
    normalized = [
        max(0.0, min(1.0, float(value)))
        for value in (crop_box or [])
    ]
    if len(normalized) != 4:
        raise ValueError("Crop box must contain four normalized values.")
    return normalized


def _serialize_crop_box(normalized: list[float]) -> str:
    return json.dumps(
        [round(value, 6) for value in normalized],
        separators=(",", ":"),
    )


def _compose_crop_boxes(
    outer: list[float], inner: list[float]
) -> list[float]:
    """Compose two normalized [left, top, width, height] crop boxes.

    Returns the inner box's coordinates relative to the outer box's
    parent, i.e. the absolute crop region within the original image.
    """
    ol, ot, ow, oh = outer
    il, it, iw, ih = inner
    return [
        max(0.0, min(1.0, ol + il * ow)),
        max(0.0, min(1.0, ot + it * oh)),
        max(0.0, min(1.0, iw * ow)),
        max(0.0, min(1.0, ih * oh)),
    ]


def _upsert_containment(
    parent_id: int, child_id: int, serialized: str
) -> ImageContainment | None:
    """Insert a containment row, or return the existing one."""
    if parent_id == child_id:
        return None

    existing = (
        ImageContainment.query.filter_by(
            parent_id=parent_id,
            child_id=child_id,
            crop_box_json=serialized,
        )
        .order_by(ImageContainment.id.desc())
        .first()
    )
    if existing:
        return existing

    entry = ImageContainment(
        parent_id=parent_id,
        child_id=child_id,
        crop_box_json=serialized,
    )
    db.session.add(entry)
    try:
        db.session.commit()
        return entry
    except IntegrityError:
        db.session.rollback()
        return (
            ImageContainment.query.filter_by(
                parent_id=parent_id,
                child_id=child_id,
                crop_box_json=serialized,
            )
            .order_by(ImageContainment.id.desc())
            .first()
        )


def save_containment_link(
    parent_id: int,
    child_id: int,
    crop_box: Sequence[float],
) -> ImageContainment | None:
    """Persist a parent/child containment relationship.

    Also creates transitive links to ancestors so that the child
    appears in every ancestor's Cropped Regions section.  The ancestry
    chain is capped at ``_MAX_CONTAINMENT_DEPTH`` levels to prevent
    unbounded propagation.
    """
    normalized = _normalize_crop_box(crop_box)
    serialized = _serialize_crop_box(normalized)

    direct = _upsert_containment(parent_id, child_id, serialized)

    # Walk up the ancestry chain and create transitive links.
    # Each iteration composes the crop box so the coordinates are
    # relative to the ancestor, not the intermediate parent.
    composed = normalized
    current_id = parent_id
    for _ in range(_MAX_CONTAINMENT_DEPTH - 1):
        ancestor_row = (
            ImageContainment.query
            .filter_by(child_id=current_id)
            .first()
        )
        if ancestor_row is None:
            break

        try:
            ancestor_box = json.loads(ancestor_row.crop_box_json)
        except (json.JSONDecodeError, TypeError):
            break

        composed = _compose_crop_boxes(ancestor_box, composed)
        _upsert_containment(
            ancestor_row.parent_id,
            child_id,
            _serialize_crop_box(composed),
        )
        current_id = ancestor_row.parent_id

    return direct


def get_displayable_containments(parent_id: int) -> list[dict[str, Any]]:
    """Return only containments that reference known child entities."""
    rows = (
        ImageContainment.query.options(
            joinedload(ImageContainment.child)
            .joinedload(ImageRegistry.consensus),
            joinedload(ImageContainment.child).joinedload(ImageRegistry.facts),
        )
        .filter_by(parent_id=parent_id)
        .order_by(ImageContainment.created_at.desc())
        .all()
    )

    visible: list[dict[str, Any]] = []
    for row in rows:
        child = row.child
        if child is None:
            continue

        consensus = getattr(child, "consensus", None)
        vote_real = getattr(consensus, "vote_real", 0) or 0
        vote_edited = getattr(consensus, "vote_edited", 0) or 0
        vote_ai = getattr(consensus, "vote_ai", 0) or 0
        total_votes = vote_real + vote_edited + vote_ai
        fact_count = len(child.facts or [])

        if total_votes == 0 and fact_count == 0:
            continue

        try:
            crop_box = json.loads(row.crop_box_json)
        except json.JSONDecodeError:
            continue

        visible.append(
            {
                "id": row.id,
                "crop_box": crop_box,
                "vote_real": vote_real,
                "vote_edited": vote_edited,
                "vote_ai": vote_ai,
                "fact_count": fact_count,
                "child_registry_id": child.id,
                "created_at": row.created_at,
            }
        )

    return visible

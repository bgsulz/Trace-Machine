from __future__ import annotations

import json
from typing import Any, Sequence

from sqlalchemy.orm import joinedload

from . import db
from .models import ImageContainment, ImageRegistry


def save_containment_link(
    parent_id: int,
    child_id: int,
    crop_box: Sequence[float],
) -> ImageContainment | None:
    """Persist a parent/child containment relationship."""
    if parent_id == child_id:
        return None

    normalized = [
        max(0.0, min(1.0, float(value)))
        for value in (crop_box or [])
    ]
    if len(normalized) != 4:
        raise ValueError("Crop box must contain four normalized values.")

    serialized = json.dumps(
        [round(value, 6) for value in normalized],
        separators=(",", ":"),
    )

    entry = ImageContainment(
        parent_id=parent_id,
        child_id=child_id,
        crop_box_json=serialized,
    )
    db.session.add(entry)
    db.session.commit()
    return entry


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

from __future__ import annotations

import logging
from io import BytesIO

import imagehash
from PIL import Image, UnidentifiedImageError

from ..models import ImageConsensus

logger = logging.getLogger(__name__)


_MAX_FUZZY_ROWS = 10_000
_MAX_HAMMING_DISTANCE = 4


def run_human_consensus(image_bytes: bytes) -> dict[str, object]:
    """Look up human consensus votes for an image via perceptual hashing.

    Strategy (MUB, small DB assumptions):
    - Compute a perceptual hash for the input image.
    - Try an exact match lookup first (fast path).
    - If no exact match, fall back to a simple Python-side fuzzy search
      over existing ImageConsensus rows using Hamming distance.
    """

    try:
        with Image.open(BytesIO(image_bytes)) as img:
            target_hash = imagehash.phash(img)
    except (UnidentifiedImageError, OSError) as exc:  # pragma: no cover - defensive
        logger.exception("Human consensus analyzer failed")
        return {
            "status": "ERROR",
            "summary": f"Failed to compute perceptual hash: {exc}",
            "data": {},
        }

    target_hex = str(target_hash)

    matches = _find_fuzzy_matches(target_hash)

    totals = {
        "vote_real": sum(entry["vote_real"] for entry in matches),
        "vote_ai": sum(entry["vote_ai"] for entry in matches),
    }
    totals["total_votes"] = totals["vote_real"] + totals["vote_ai"]

    if matches:
        summary = (
            f"{len(matches)} consensus entries within distance ≤ "
            f"{_MAX_HAMMING_DISTANCE}. "
            f"Combined votes: {totals['vote_ai']} AI / {totals['vote_real']} Real"
        )
        status = "FOUND"
    else:
        summary = "No community consensus yet."
        status = "NO DATA"

    return {
        "status": status,
        "summary": summary,
        "data": {
            "phash": target_hex,
            "matches": matches,
            "totals": totals,
            "threshold": _MAX_HAMMING_DISTANCE,
        },
    }


def _find_fuzzy_matches(target_hash: imagehash.ImageHash) -> list[dict[str, object]]:
    """Return all consensus rows within the Hamming threshold."""

    try:
        rows = (
            ImageConsensus.query.order_by(ImageConsensus.created_at.desc())
            .limit(_MAX_FUZZY_ROWS)
            .all()
        )
    except Exception:  # pragma: no cover - defensive
        logger.exception("Human consensus fuzzy query failed")
        return []

    matches: list[dict[str, object]] = []
    for row in rows:
        try:
            row_hash = imagehash.hex_to_hash(row.phash)
        except Exception:  # pragma: no cover - skip bad rows
            continue

        distance = int(target_hash - row_hash)
        if distance > _MAX_HAMMING_DISTANCE:
            continue

        total_votes = (row.vote_real or 0) + (row.vote_ai or 0)
        matches.append(
            {
                "phash": row.phash,
                "distance": distance,
                "vote_real": row.vote_real,
                "vote_ai": row.vote_ai,
                "total_votes": total_votes,
                "created_at": row.created_at.isoformat() if row.created_at else "",
            }
        )

    matches.sort(key=lambda entry: (entry["distance"], entry["created_at"]))
    return matches

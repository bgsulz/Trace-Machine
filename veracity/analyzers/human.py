from __future__ import annotations

import logging
from io import BytesIO

import imagehash
from PIL import Image, UnidentifiedImageError

from ..models import ImageConsensus, ImageSource

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

    # Attach up to a small number of known source URLs for each nearby hash.
    phashes = [entry["phash"] for entry in matches]
    sources_by_phash: dict[str, list[dict[str, str]]] = {}
    if phashes:
        try:
            rows = (
                ImageSource.query.filter(ImageSource.phash.in_(phashes))
                .order_by(ImageSource.phash, ImageSource.created_at.desc())
                .all()
            )
        except Exception:  # pragma: no cover - defensive
            logger.exception("Human consensus source lookup failed")
            rows = []

        for row in rows:
            bucket = sources_by_phash.setdefault(row.phash, [])
            if len(bucket) < 3:
                bucket.append({"url": row.url})

    for entry in matches:
        entry["sources"] = sources_by_phash.get(entry["phash"], [])

    totals = {
        "vote_real": sum(entry["vote_real"] for entry in matches),
        "vote_edited": sum(entry["vote_edited"] for entry in matches),
        "vote_ai": sum(entry["vote_ai"] for entry in matches),
    }
    totals["total_votes"] = (
        totals["vote_real"] + totals["vote_edited"] + totals["vote_ai"]
    )

    has_matches = bool(matches)
    total_votes = totals["total_votes"]

    if has_matches:
        matches_summary = (
            f"Showing {len(matches)} matches (distance ≤ {_MAX_HAMMING_DISTANCE}). "
            f"Combined votes: Real {totals['vote_real']} / "
            f"AI-edited {totals['vote_edited']} / "
            f"AI {totals['vote_ai']} "
            f"(total {total_votes})."
        )
    else:
        matches_summary = ""

    no_votes_message = "No votes yet."

    if matches:
        summary = (
            f"{len(matches)} consensus entries within distance ≤ "
            f"{_MAX_HAMMING_DISTANCE}. "
            f"Combined votes: Real {totals['vote_real']} / "
            f"AI-edited {totals['vote_edited']} / "
            f"AI {totals['vote_ai']}"
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
            "has_matches": has_matches,
            "matches_summary": matches_summary,
            "no_votes_message": no_votes_message,
        },
    }


def _find_fuzzy_matches(target_hash: imagehash.ImageHash) -> list[dict[str, object]]:
    """Return all consensus rows within the Hamming threshold."""

    rows = (
        ImageConsensus.query.order_by(ImageConsensus.created_at.desc())
        .limit(_MAX_FUZZY_ROWS)
        .all()
    )

    matches: list[dict[str, object]] = []
    for row in rows:
        try:
            row_hash = imagehash.hex_to_hash(row.phash)
        except Exception:  # pragma: no cover - skip bad rows
            continue

        distance = int(target_hash - row_hash)
        if distance > _MAX_HAMMING_DISTANCE:
            continue

        total_votes = (row.vote_real or 0) + (row.vote_edited or 0) + (row.vote_ai or 0)
        matches.append(
            {
                "phash": row.phash,
                "distance": distance,
                "vote_real": row.vote_real,
                "vote_edited": row.vote_edited,
                "vote_ai": row.vote_ai,
                "total_votes": total_votes,
                "created_at": row.created_at.isoformat() if row.created_at else "",
            }
        )

    matches.sort(key=lambda entry: (entry["distance"], entry["created_at"]))
    return matches

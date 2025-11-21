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

    # 1) Exact match (O(1))
    match = ImageConsensus.query.filter_by(phash=target_hex).first()
    if match:
        summary = f"Consensus: {match.vote_ai} AI / {match.vote_real} Real"
        return {
            "status": "FOUND",
            "summary": summary,
            "data": {
                "phash": target_hex,
                "vote_ai": match.vote_ai,
                "vote_real": match.vote_real,
                "total_votes": match.vote_ai + match.vote_real,
                "distance": 0,
            },
        }

    # 2) Fuzzy search (O(N)) for small N
    candidate = _find_best_fuzzy_match(target_hex)
    if candidate is None:
        return {
            "status": "NO DATA",
            "summary": "No community consensus yet.",
            "data": {"phash": target_hex, "matches": 0},
        }

    row, distance = candidate
    summary = (
        f"Approximate match (distance={distance}): "
        f"{row.vote_ai} AI / {row.vote_real} Real"
    )
    return {
        "status": "FOUND",
        "summary": summary,
        "data": {
            "phash": target_hex,
            "matched_phash": row.phash,
            "vote_ai": row.vote_ai,
            "vote_real": row.vote_real,
            "total_votes": row.vote_ai + row.vote_real,
            "distance": distance,
        },
    }


def _find_best_fuzzy_match(target_hex: str) -> tuple[ImageConsensus, int] | None:
    """Return (row, distance) for the closest hash within the threshold.

    Fetches at most _MAX_FUZZY_ROWS rows to keep the prototype simple.
    """

    try:
        rows = (
            ImageConsensus.query.order_by(ImageConsensus.created_at.desc())
            .limit(_MAX_FUZZY_ROWS)
            .all()
        )
    except Exception:  # pragma: no cover - defensive
        logger.exception("Human consensus fuzzy query failed")
        return None

    if not rows:
        return None

    try:
        target_hash = imagehash.hex_to_hash(target_hex)
    except Exception:  # pragma: no cover - defensive
        logger.exception("Invalid target hash for fuzzy comparison: %s", target_hex)
        return None

    best_row: ImageConsensus | None = None
    best_distance: int | None = None

    for row in rows:
        try:
            row_hash = imagehash.hex_to_hash(row.phash)
        except Exception:  # pragma: no cover - skip bad rows
            continue

        distance = target_hash - row_hash
        if best_distance is None or distance < best_distance:
            best_row = row
            best_distance = distance

    if best_row is None or best_distance is None:
        return None

    if best_distance > _MAX_HAMMING_DISTANCE:
        return None

    return best_row, best_distance

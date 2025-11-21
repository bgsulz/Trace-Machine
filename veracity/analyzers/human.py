from __future__ import annotations

import logging
from io import BytesIO

import imagehash
from PIL import Image, UnidentifiedImageError

from ..models import ImageConsensus

logger = logging.getLogger(__name__)


def run_human_consensus(image_bytes: bytes) -> dict[str, object]:
    """Look up human consensus votes for an image via perceptual hashing."""

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

    match = ImageConsensus.query.filter_by(phash=target_hex).first()

    if not match:
        return {
            "status": "NO DATA",
            "summary": "No community consensus yet.",
            "data": {"phash": target_hex, "matches": 0},
        }

    summary = f"Consensus: {match.vote_ai} AI / {match.vote_real} Real"
    return {
        "status": "FOUND",
        "summary": summary,
        "data": {
            "phash": target_hex,
            "vote_ai": match.vote_ai,
            "vote_real": match.vote_real,
            "total_votes": match.vote_ai + match.vote_real,
        },
    }

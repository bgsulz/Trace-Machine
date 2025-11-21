from __future__ import annotations

import logging
from io import BytesIO

import imagehash
from PIL import Image, UnidentifiedImageError

logger = logging.getLogger(__name__)


def run_human_consensus(image_bytes: bytes) -> dict[str, object]:
    """Compute a perceptual hash via ImageHash.

    Database wiring (votes, Hamming search) lands in Phase 4.
    """
    try:
        with Image.open(BytesIO(image_bytes)) as img:
            hash_value = imagehash.phash(img)
    except (UnidentifiedImageError, OSError) as exc:  # pragma: no cover - defensive
        logger.exception("Human consensus analyzer failed")
        return {
            "status": "ERROR",
            "summary": f"Failed to compute perceptual hash: {exc}",
            "data": {},
        }

    summary = f"phash={hash_value} (consensus DB pending)"
    return {
        "status": "HASHED",
        "summary": summary,
        "data": {"phash": str(hash_value)},
    }

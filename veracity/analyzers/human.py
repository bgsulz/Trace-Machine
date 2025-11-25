from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import imagehash

from ..models import ImageConsensus

if TYPE_CHECKING:  # pragma: no cover - type checking only
    from .manager import AnalysisContext

logger = logging.getLogger(__name__)


_MAX_FUZZY_ROWS = 10_000
_MAX_HAMMING_DISTANCE = 4


def run_human_consensus(context: "AnalysisContext") -> dict[str, object]:
    """Look up human consensus votes for an image via perceptual hashing.

    Strategy (MUB, small DB assumptions):
    - Compute a perceptual hash for the input image.
    - Try an exact match lookup first (fast path).
    - If no exact match, fall back to a simple Python-side fuzzy search
      over existing ImageConsensus rows using Hamming distance.
    """

    target_hex = context.phash

    matches: list[dict[str, object]] = []

    for neighbor in context.neighbors:
        consensus = getattr(neighbor, "consensus", None)
        if not consensus:
            continue

        try:
            base_hash = imagehash.hex_to_hash(context.phash)
            neighbor_hash = imagehash.hex_to_hash(neighbor.phash)
            distance = int(base_hash - neighbor_hash)
        except Exception:  # pragma: no cover - defensive
            distance = 0

        total_votes = (
            (consensus.vote_real or 0)
            + (consensus.vote_edited or 0)
            + (consensus.vote_ai or 0)
        )

        sources = []
        for src in getattr(neighbor, "sources", [])[:3]:
            sources.append({"url": src.url})

        matches.append(
            {
                "phash": neighbor.phash,
                "distance": distance,
                "vote_real": consensus.vote_real,
                "vote_edited": consensus.vote_edited,
                "vote_ai": consensus.vote_ai,
                "total_votes": total_votes,
                "created_at": neighbor.created_at.isoformat()
                if getattr(neighbor, "created_at", None)
                else "",
                "sources": sources,
            }
        )

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
        matches_summary = f"Showing matches with distance ≤ {_MAX_HAMMING_DISTANCE}:"
    else:
        matches_summary = ""

    no_votes_message = "No votes yet."

    if matches:
        summary = (
            f"{len(matches)} similar images with {total_votes} votes total. "
            f"Real {totals['vote_real']} / "
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

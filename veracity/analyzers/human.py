from __future__ import annotations
import logging
import imagehash
from .context import AnalysisContext

logger = logging.getLogger(__name__)


_MAX_FUZZY_ROWS = 10_000
_MAX_HAMMING_DISTANCE = 4


def run_human_consensus(context: AnalysisContext) -> dict[str, object]:
    """Look up human consensus votes for an image via perceptual hashing.

    Strategy (MUB, small DB assumptions):
    - Compute a perceptual hash for the input image.
    - Try an exact match lookup first (fast path).
    - If no exact match, fall back to a simple Python-side fuzzy search
      over existing ImageConsensus rows using Hamming distance.
    """

    target_hex = context.phash
    target_whash = context.whash

    matches: list[dict[str, object]] = []

    try:
        base_hash = imagehash.hex_to_hash(target_hex)
    except Exception:  # pragma: no cover - defensive
        base_hash = None

    try:
        base_whash = imagehash.hex_to_hash(target_whash)
    except Exception:  # pragma: no cover - defensive
        base_whash = None

    for neighbor in context.neighbors:
        consensus = getattr(neighbor, "consensus", None)
        if not consensus:
            continue

        phash_distance: int | None = None
        whash_distance: int | None = None

        try:
            neighbor_hash = imagehash.hex_to_hash(neighbor.phash)
            phash_distance = int(base_hash - neighbor_hash) if base_hash else None
        except Exception:  # pragma: no cover - defensive
            phash_distance = None

        neighbor_whash_val = getattr(neighbor, "whash", None)
        if neighbor_whash_val:
            try:
                neighbor_whash = imagehash.hex_to_hash(neighbor_whash_val)
                if base_whash is not None:
                    whash_distance = int(base_whash - neighbor_whash)
            except Exception:  # pragma: no cover - defensive
                whash_distance = None

        total_votes = (
            (consensus.vote_real or 0)
            + (consensus.vote_edited or 0)
            + (consensus.vote_ai or 0)
        )

        sources = []
        for src in getattr(neighbor, "sources", [])[:3]:
            sources.append({"url": src.url})

        # Prefer the hash type that matched neighbor inclusion; fall back to phash.
        display_hash = neighbor.phash
        display_label = "phash"
        display_distance = phash_distance if phash_distance is not None else 0

        if whash_distance is not None and (
            phash_distance is None or whash_distance <= phash_distance
        ):
            display_hash = neighbor_whash_val
            display_label = "whash"
            display_distance = whash_distance

        matches.append(
            {
                "phash": neighbor.phash,
                "whash": neighbor_whash_val,
                "hash_display": f"{display_hash} ({display_label})",
                "distance": display_distance,
                "distance_phash": phash_distance,
                "distance_whash": whash_distance,
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
        matches_summary = "Similar images with votes:"
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
        status = "NOT FOUND"

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

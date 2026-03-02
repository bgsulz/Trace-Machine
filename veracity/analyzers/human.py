from __future__ import annotations
import logging
from .context import AnalysisContext
from .hash_utils import (
    compute_base_hashes,
    compute_neighbor_distances,
    extract_sources,
    format_hash_display,
    local_match_payload,
    match_method_label,
)

logger = logging.getLogger(__name__)


_MAX_HAMMING_DISTANCE = 4

_SEGMENT_LABELS = {
    "real": "Real",
    "edited": "AI-Edited",
    "ai": "AI-Gen",
}


def _build_vote_breakdown(*, real: int | None, edited: int | None, ai: int | None) -> dict[str, object]:
    counts = {
        "real": real or 0,
        "edited": edited or 0,
        "ai": ai or 0,
    }
    total = counts["real"] + counts["edited"] + counts["ai"]

    segments: list[dict[str, object]] = []
    for key in ("real", "edited", "ai"):
        value = counts[key]
        percent = (value / total * 100) if total else 0
        segments.append(
            {
                "kind": key,
                "label": _SEGMENT_LABELS[key],
                "count": value,
                "percent": percent,
            }
        )

    return {"total": total, "segments": segments, "counts": counts}


def run_human_consensus(context: AnalysisContext) -> dict[str, object]:
    """Aggregate human consensus from matched neighbors in analysis context."""

    target_hex = context.phash
    matches: list[dict[str, object]] = []

    base_phash, base_whash = compute_base_hashes(context.phash, context.whash)

    for neighbor in context.neighbors:
        consensus = getattr(neighbor, "consensus", None)
        if not consensus:
            continue

        neighbor_id = getattr(neighbor, "id", None)
        is_self_match = neighbor_id == context.registry_id if neighbor_id is not None else False
        neighbor_phash = getattr(neighbor, "phash", None)
        if not neighbor_phash:
            continue

        neighbor_whash_val = getattr(neighbor, "whash", None)
        match_method = str(getattr(neighbor, "match_method", "hash") or "hash").lower()
        local_snapshot = getattr(neighbor, "local_match", None)
        (
            phash_distance,
            whash_distance,
            display_hash,
            display_label,
            display_distance,
        ) = compute_neighbor_distances(
            base_phash, base_whash, neighbor_phash, neighbor_whash_val
        )
        distance_display = display_distance if match_method != "local" else None

        total_votes = (
            (consensus.vote_real or 0)
            + (consensus.vote_edited or 0)
            + (consensus.vote_ai or 0)
        )

        sources = extract_sources(neighbor)
        local = local_match_payload(local_snapshot, include_homography=True)

        # Prefer the hash type that matched neighbor inclusion; fall back to phash.
        matches.append(
            {
                "phash": neighbor_phash,
                "whash": neighbor_whash_val,
                "image_id": neighbor_id,
                "is_self_match": is_self_match,
                "hash_display": format_hash_display(
                    display_hash,
                    display_label,
                    neighbor_phash,
                ),
                "distance": distance_display,
                "distance_phash": phash_distance,
                "distance_whash": whash_distance,
                "vote_real": consensus.vote_real,
                "vote_edited": consensus.vote_edited,
                "vote_ai": consensus.vote_ai,
                "match_method": match_method,
                "match_method_label": match_method_label(match_method),
                "local": local,
                "vote_breakdown": _build_vote_breakdown(
                    real=consensus.vote_real, edited=consensus.vote_edited, ai=consensus.vote_ai
                ),
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
    distant_match_count = sum(1 for entry in matches if not entry.get("is_self_match"))
    has_distant_matches = distant_match_count > 0
    total_votes = totals["total_votes"]
    local_match_count = sum(1 for entry in matches if entry.get("local"))

    if has_distant_matches:
        matches_summary = (
            "Similar image with votes:"
            if distant_match_count == 1
            else "Similar images with votes:"
        )
    else:
        matches_summary = ""

    direct_only_message = "Votes are recorded on this image only."
    no_votes_message = "No votes yet."

    if has_distant_matches:
        summary = (
            f"{distant_match_count} similar image{'s' if distant_match_count != 1 else ''} "
            f"with {total_votes} votes total."
        )
        status = "FOUND"
    elif has_matches:
        summary = f"{total_votes} direct vote{'s' if total_votes != 1 else ''} recorded."
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
            "overall_breakdown": _build_vote_breakdown(
                real=totals["vote_real"], edited=totals["vote_edited"], ai=totals["vote_ai"]
            ),
            "threshold": _MAX_HAMMING_DISTANCE,
            "has_matches": has_matches,
            "has_distant_matches": has_distant_matches,
            "distant_match_count": distant_match_count,
            "matches_summary": matches_summary,
            "direct_only_message": direct_only_message,
            "local_match_count": local_match_count,
            "no_votes_message": no_votes_message,
        },
    }

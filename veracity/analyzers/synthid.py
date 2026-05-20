"""SynthID analyzer with community reporting.

SynthID is invisible watermarking technology for AI-generated images. Public
detectors are provider-specific, so this analyzer gives users manual check paths
and aggregates community reports from users who checked.
"""

from __future__ import annotations

from .context import AnalysisContext
from .hash_utils import (
    iter_neighbor_views,
)
from ..services.synthid_service import SYNTHID_DETECTORS


# Tier weights for gating score
_WEIGHT_SAME_ENTRY = 1.0
_WEIGHT_SAME_HASH = 0.75
_WEIGHT_SIMILAR = 0.5

# Gating threshold for DETECTED state
_DETECTED_THRESHOLD = 4

# Tier A contradiction ratio: not_detected >= 3 * detected zeroes contribution
_CONTRADICTION_RATIO = 3


def run_synthid(context: AnalysisContext) -> dict[str, object]:
    """Scan neighbors for SynthID reports and compute gating score."""
    this_image = _empty_counts()
    similar_images: list[dict[str, object]] = []
    totals_by_detector = _empty_detector_counts()
    score = 0.0
    any_reports = False
    tier_a_detected = 0
    tier_a_not_detected = 0

    for neighbor_view in iter_neighbor_views(context):
        neighbor = neighbor_view["neighbor"]
        synthid = getattr(neighbor, "synthid", None)
        if synthid is None:
            continue

        detected = synthid.detected
        not_detected = synthid.not_detected
        by_detector = getattr(synthid, "by_detector", {}) or {}
        if detected == 0 and not_detected == 0:
            continue

        any_reports = True
        _add_detector_counts(totals_by_detector, by_detector)
        phash_dist = neighbor_view["phash_distance"]
        whash_dist = neighbor_view["whash_distance"]

        # Classify tier
        min_dist = _min_distance(phash_dist, whash_dist)
        if neighbor_view["is_self_match"]:
            # Tier A: same entry
            weight = _WEIGHT_SAME_ENTRY
            tier_a_detected += detected
            tier_a_not_detected += not_detected
            this_image["detected"] += detected
            this_image["not_detected"] += not_detected
            _add_detector_counts(this_image["by_detector"], by_detector)

            # Tier A contradiction rule
            contribution = detected
            if not_detected >= _CONTRADICTION_RATIO * detected and detected > 0:
                contribution = 0
            score += weight * contribution
        elif min_dist == 0:
            # Tier B: same perceptual hash, different entry
            weight = _WEIGHT_SAME_HASH
            score += weight * detected
            _append_similar(
                similar_images,
                neighbor_view,
                detected,
                not_detected,
                by_detector,
            )
        else:
            # Tier C: similar (within neighbor threshold)
            weight = _WEIGHT_SIMILAR
            score += weight * detected
            _append_similar(
                similar_images,
                neighbor_view,
                detected,
                not_detected,
                by_detector,
            )

    # Determine contested flag
    contested = False
    if tier_a_detected > 0 and tier_a_not_detected > 0:
        ratio = tier_a_not_detected / tier_a_detected
        if 1.0 <= ratio <= _CONTRADICTION_RATIO:
            contested = True

    # Determine display state and build output
    total_detected = this_image["detected"] + sum(
        s["detected"] for s in similar_images
    )
    total_not_detected = this_image["not_detected"] + sum(
        s["not_detected"] for s in similar_images
    )
    totals = {"detected": total_detected, "not_detected": total_not_detected}
    checker_rows = _build_checker_rows(totals_by_detector)

    if score == 0 and not any_reports:
        display_state = "manual"
        status = "MANUAL"
        summary = "Check for invisible SynthID watermarking."
        caveat = None
    elif score == 0:
        display_state = "checked"
        status = "CHECKED"
        total_reporters = total_detected + total_not_detected
        summary = (
            f"Checked by {total_reporters} "
            f"user{'s' if total_reporters != 1 else ''}, "
            f"not detected on this version."
        )
        caveat = None
    elif score < _DETECTED_THRESHOLD:
        display_state = "reported"
        status = "REPORTED"
        only_similar = this_image["detected"] == 0 and total_detected > 0
        if only_similar:
            summary = (
                f"{total_detected} user{'s' if total_detected != 1 else ''} "
                f"reported detecting SynthID on a similar image."
            )
        else:
            summary = (
                f"{total_detected} user{'s' if total_detected != 1 else ''} "
                f"reported detecting SynthID."
            )
        caveat = (
            "Verify this yourself; SynthID checks are provider-specific "
            "and can vary across different copies of an image."
        )
    else:
        display_state = "detected"
        status = "DETECTED"
        summary = (
            f"SynthID detected by {total_detected} "
            f"user{'s' if total_detected != 1 else ''}."
        )
        caveat = None

    return {
        "status": status,
        "summary": summary,
        "data": {
            "header_action": {"type": "open_all", "label": "Open All"},
            "display_state": display_state,
            "contested": contested,
            "this_image": this_image,
            "similar_images": similar_images,
            "has_distant_matches": bool(similar_images),
            "totals": totals,
            "by_detector": totals_by_detector,
            "checker_rows": checker_rows,
            "score": score,
            "caveat": caveat,
        },
    }


def _min_distance(phash_dist: int | None, whash_dist: int | None) -> int | None:
    if phash_dist is not None and whash_dist is not None:
        return min(phash_dist, whash_dist)
    return phash_dist if phash_dist is not None else whash_dist


def _append_similar(
    similar_images: list[dict[str, object]],
    neighbor_view: dict[str, object],
    detected: int,
    not_detected: int,
    by_detector: dict[str, dict[str, object]],
) -> None:
    similar_images.append({
        "phash": neighbor_view["phash"],
        "whash": neighbor_view["whash"],
        "hash_display": neighbor_view["hash_display"],
        "distance": neighbor_view["display_distance"],
        "detected": detected,
        "not_detected": not_detected,
        "by_detector": by_detector,
        "sources": neighbor_view["sources"],
    })


def _empty_counts() -> dict[str, object]:
    return {
        "detected": 0,
        "not_detected": 0,
        "by_detector": _empty_detector_counts(),
    }


def _empty_detector_counts() -> dict[str, dict[str, object]]:
    return {
        detector: {
            "provider": spec["provider"],
            "detector": detector,
            "detected": 0,
            "not_detected": 0,
            "total": 0,
        }
        for detector, spec in SYNTHID_DETECTORS.items()
    }


def _add_detector_counts(
    target: dict[str, dict[str, object]],
    source: dict[str, dict[str, object]],
) -> None:
    for detector, counts in source.items():
        spec = SYNTHID_DETECTORS.get(detector, {})
        row = target.setdefault(
            detector,
            {
                "provider": counts.get("provider") or spec.get("provider") or "unknown",
                "detector": detector,
                "detected": 0,
                "not_detected": 0,
                "total": 0,
            },
        )
        row["detected"] = int(row.get("detected") or 0) + int(counts.get("detected") or 0)
        row["not_detected"] = int(row.get("not_detected") or 0) + int(
            counts.get("not_detected") or 0
        )
        row["total"] = int(row["detected"]) + int(row["not_detected"])


def _build_checker_rows(
    by_detector: dict[str, dict[str, object]]
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for detector, spec in SYNTHID_DETECTORS.items():
        counts = by_detector.get(detector) or {}
        detected = int(counts.get("detected") or 0)
        not_detected = int(counts.get("not_detected") or 0)
        rows.append(
            {
                "provider": spec["provider"],
                "detector": detector,
                "label": spec["label"],
                "short_label": spec["short_label"],
                "check_label": spec["check_label"],
                "detected": detected,
                "not_detected": not_detected,
                "total": detected + not_detected,
            }
        )
    return rows

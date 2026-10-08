"""Verification portal analyzer with community reporting.

The internal slug remains ``synthid`` for compatibility, but this analyzer now
tracks user reports from provider verification portals rather than claiming to
decode SynthID locally.
"""

from __future__ import annotations

from .context import AnalysisContext
from .hash_utils import iter_neighbor_views
from datetime import datetime, timezone

from ..services.openai_verify_service import REUSE_WINDOW, results_from_facts
from ..services.synthid_service import (
    GOOGLE_POSITIVE,
    META_POSITIVE,
    NEGATIVE,
    OPENAI_POSITIVE,
    PORTAL_RESULTS,
    portal_payload_from_counts,
)


_WEIGHT_SAME_ENTRY = 1.0
_WEIGHT_SAME_HASH = 0.75
_WEIGHT_SIMILAR = 0.5
_DETECTED_THRESHOLD = 4
_CONTRADICTION_RATIO = 3


def run_synthid(context: AnalysisContext) -> dict[str, object]:
    this_image = _empty_counts()
    similar_images: list[dict[str, object]] = []
    totals = _empty_counts()
    score_by_result = {
        GOOGLE_POSITIVE: 0.0,
        OPENAI_POSITIVE: 0.0,
        META_POSITIVE: 0.0,
    }
    any_reports = False
    tier_a_positive = 0
    tier_a_negative = 0

    for neighbor_view in iter_neighbor_views(context):
        neighbor = neighbor_view["neighbor"]
        synthid = getattr(neighbor, "synthid", None)
        if synthid is None:
            continue

        counts = _counts_from_snapshot(synthid)
        if _total_reports(counts) == 0:
            continue

        any_reports = True
        _add_counts(totals, counts)
        phash_dist = neighbor_view["phash_distance"]
        whash_dist = neighbor_view["whash_distance"]
        weight = _weight_for_neighbor(neighbor_view, phash_dist, whash_dist)

        if neighbor_view["is_self_match"]:
            _add_counts(this_image, counts)
            tier_a_positive += sum(
                counts[result] for result in score_by_result
            )
            tier_a_negative += counts[NEGATIVE]
            contribution = counts
            if (
                tier_a_positive > 0
                and tier_a_negative >= _CONTRADICTION_RATIO * tier_a_positive
            ):
                contribution = {
                    **counts,
                    GOOGLE_POSITIVE: 0,
                    OPENAI_POSITIVE: 0,
                    META_POSITIVE: 0,
                }
            _add_weighted_scores(score_by_result, contribution, weight)
        else:
            _add_weighted_scores(score_by_result, counts, weight)
            _append_similar(similar_images, neighbor_view, counts)

    # An automated OpenAI check on this exact image outranks community reports
    # about OpenAI: a clean negative (sent unconverted, so both C2PA and
    # SynthID were checked) rules out OpenAI-positive reports.
    automated = _automated_result(context)
    own_automated = automated if automated and automated["source"] == "this image" else None
    if own_automated and not own_automated["detected"] and not own_automated.get("converted"):
        score_by_result[OPENAI_POSITIVE] = 0.0

    positive_results = [
        result
        for result, score in score_by_result.items()
        if score > 0
    ]
    contested = len(positive_results) > 1
    portal_payload = portal_payload_from_counts(totals)
    checker_rows = _build_checker_rows(totals)

    if not any_reports:
        display_state = "manual"
        status = "MANUAL"
        summary = "Check external verification portals."
        caveat = None
    elif contested:
        display_state = "contested"
        status = "REPORTED"
        summary = "Conflicting provider-positive portal reports."
        caveat = (
            "A single image should not be positive for more than one provider "
            "portal. Treat this as conflicting community evidence."
        )
    elif not positive_results:
        display_state = "checked"
        status = "CHECKED"
        total_reporters = _total_reports(totals)
        summary = (
            f"Checked by {total_reporters} "
            f"user{'s' if total_reporters != 1 else ''}, "
            "with no portal-positive reports on this version."
        )
        caveat = None
    else:
        result = positive_results[0]
        score = score_by_result[result]
        label = PORTAL_RESULTS[result]["short_label"]
        display_state = "detected" if score >= _DETECTED_THRESHOLD else "reported"
        status = "DETECTED" if score >= _DETECTED_THRESHOLD else "REPORTED"
        total = totals[result]
        only_similar = this_image[result] == 0 and total > 0
        source = " on a similar image" if only_similar else ""
        summary = (
            f"{total} user{'s' if total != 1 else ''} "
            f"reported a portal naming {label} as the maker{source}."
        )
        caveat = None if status == "DETECTED" else (
            "Verify this yourself; portal checks are provider-specific and can "
            "vary across different copies of an image."
        )

    # A positive automated check on this exact image decides the status. One
    # on a near-identical copy is only a lead: the copy may be an AI-edited
    # variant of an authentic photo, so it must not mark this image detected.
    if automated and automated["detected"]:
        found = " and ".join(
            label
            for key, label in (("synthid", "a SynthID watermark"), ("c2pa", "OpenAI Content Credentials"))
            if (automated.get(key) or {}).get("detected")
        )
        if own_automated:
            status, display_state, contested, caveat = "DETECTED", "detected", False, None
            summary = f"OpenAI's API detected {found} on this image."
        elif status not in ("DETECTED",):
            status, display_state = "REPORTED", "reported"
            summary = f"OpenAI's API detected {found} on a near-identical copy."
            caveat = (
                "A near-identical copy tested positive. That copy may be an edited "
                "variant, so check this image itself."
            )
    elif own_automated and status in ("MANUAL", "CHECKED"):
        summary = "OpenAI's API found no OpenAI signals. Google and Meta still need a manual check."

    return {
        "status": status,
        "summary": summary,
        "data": {
            "automated": automated,
            "header_action": {"type": "verification_portals"},
            "display_state": display_state,
            "contested": contested,
            "this_image": this_image,
            "similar_images": similar_images,
            "has_distant_matches": bool(similar_images),
            "totals": totals,
            "verification_portals": portal_payload,
            "checker_rows": checker_rows,
            "scores": score_by_result,
            "score": max(score_by_result.values(), default=0.0),
            "caveat": caveat,
        },
    }


def _automated_result(context: AnalysisContext) -> dict[str, object] | None:
    """The most relevant stored OpenAI API result for this image.

    This image's own latest check wins; otherwise a positive check on a
    near-identical copy; otherwise any check on a copy.
    """
    own = None
    similar: list[dict[str, object]] = []
    for view in iter_neighbor_views(context):
        results = results_from_facts(getattr(view["neighbor"], "facts", None))
        if not results:
            continue
        if view["is_self_match"]:
            own = {**results[0], "source": "this image"}
        elif str(view.get("match_method") or "hash") != "local":
            # Feature-point-only matches (crops, composites) aren't copies.
            similar.append({**results[0], "source": "a similar image"})
    if own:
        try:
            checked = datetime.fromisoformat(str(own.get("checked_at")))
            own["recent"] = datetime.now(timezone.utc) - checked < REUSE_WINDOW
        except ValueError:
            own["recent"] = False
        return own
    positive = [result for result in similar if result.get("detected")]
    return (positive or similar or [None])[0]


def _empty_counts() -> dict[str, int]:
    return {
        GOOGLE_POSITIVE: 0,
        OPENAI_POSITIVE: 0,
        META_POSITIVE: 0,
        NEGATIVE: 0,
    }


def _counts_from_snapshot(synthid) -> dict[str, int]:
    return {
        GOOGLE_POSITIVE: int(getattr(synthid, GOOGLE_POSITIVE, 0) or 0),
        OPENAI_POSITIVE: int(getattr(synthid, OPENAI_POSITIVE, 0) or 0),
        META_POSITIVE: int(getattr(synthid, META_POSITIVE, 0) or 0),
        NEGATIVE: int(getattr(synthid, NEGATIVE, 0) or 0),
    }


def _total_reports(counts: dict[str, int]) -> int:
    return sum(int(value or 0) for value in counts.values())


def _add_counts(target: dict[str, int], source: dict[str, int]) -> None:
    for key in target:
        target[key] += int(source.get(key) or 0)


def _add_weighted_scores(
    scores: dict[str, float],
    counts: dict[str, int],
    weight: float,
) -> None:
    scores[GOOGLE_POSITIVE] += weight * int(counts.get(GOOGLE_POSITIVE) or 0)
    scores[OPENAI_POSITIVE] += weight * int(counts.get(OPENAI_POSITIVE) or 0)
    scores[META_POSITIVE] += weight * int(counts.get(META_POSITIVE) or 0)


def _weight_for_neighbor(
    neighbor_view: dict[str, object],
    phash_dist: int | None,
    whash_dist: int | None,
) -> float:
    if neighbor_view["is_self_match"]:
        return _WEIGHT_SAME_ENTRY
    min_dist = _min_distance(phash_dist, whash_dist)
    if min_dist == 0:
        return _WEIGHT_SAME_HASH
    return _WEIGHT_SIMILAR


def _min_distance(phash_dist: int | None, whash_dist: int | None) -> int | None:
    if phash_dist is not None and whash_dist is not None:
        return min(phash_dist, whash_dist)
    return phash_dist if phash_dist is not None else whash_dist


def _append_similar(
    similar_images: list[dict[str, object]],
    neighbor_view: dict[str, object],
    counts: dict[str, int],
) -> None:
    similar_images.append({
        "phash": neighbor_view["phash"],
        "whash": neighbor_view["whash"],
        "hash_display": neighbor_view["hash_display"],
        "distance": neighbor_view["display_distance"],
        "google_positive": counts[GOOGLE_POSITIVE],
        "openai_positive": counts[OPENAI_POSITIVE],
        "meta_positive": counts[META_POSITIVE],
        "negative": counts[NEGATIVE],
        "total": _total_reports(counts),
        "verification_portals": portal_payload_from_counts(counts),
        "sources": neighbor_view["sources"],
    })


def _build_checker_rows(counts: dict[str, int]) -> list[dict[str, object]]:
    return [
        {
            "result": result,
            "provider": spec["provider"],
            "label": spec["label"],
            "short_label": spec["short_label"],
            "count": int(counts.get(result) or 0),
        }
        for result, spec in PORTAL_RESULTS.items()
    ]

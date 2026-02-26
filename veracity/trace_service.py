from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import joinedload

from .analyzers.context import AnalysisContext
from .analyzers.hash_utils import (
    compute_base_hashes,
    compute_neighbor_distances,
    extract_sources,
)
from .models import ImageRegistry


_MATCH_METHOD_PRIORITY = {"hybrid": 0, "local": 1, "hash": 2}
_MATCH_METHOD_LABEL = {
    "hybrid": "Hybrid (hash + local)",
    "local": "Local geometric",
    "hash": "Hash",
}


def build_direct_and_distant_traces(
    context: AnalysisContext,
    *,
    analyzer_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    current_image = (
        ImageRegistry.query.options(
            joinedload(ImageRegistry.consensus),
            joinedload(ImageRegistry.facts),
            joinedload(ImageRegistry.synthid_reports),
        )
        .filter_by(id=context.registry_id)
        .first()
    )

    direct_items = _build_direct_items(current_image, analyzer_rows=analyzer_rows)
    distant_matches = _build_distant_matches(context)

    return {
        "direct_items": direct_items,
        "distant_matches": distant_matches,
        "has_any": bool(direct_items or distant_matches),
    }


def _build_direct_items(
    current_image: ImageRegistry | None,
    *,
    analyzer_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if current_image is None:
        return []

    items: list[dict[str, Any]] = []

    c2pa_facts = [
        str(fact.data)
        for fact in (current_image.facts or [])
        if getattr(fact, "analyzer", None) == "c2pa"
    ]
    if c2pa_facts:
        items.append(
            {
                "kind": "c2pa",
                "title": "Direct C2PA",
                "summary": f"{len(c2pa_facts)} direct C2PA fact{'s' if len(c2pa_facts) != 1 else ''}.",
                "details": c2pa_facts[:3],
            }
        )

    consensus = getattr(current_image, "consensus", None)
    if consensus is not None:
        vote_real = int(getattr(consensus, "vote_real", 0) or 0)
        vote_edited = int(getattr(consensus, "vote_edited", 0) or 0)
        vote_ai = int(getattr(consensus, "vote_ai", 0) or 0)
        total_votes = vote_real + vote_edited + vote_ai
        if total_votes > 0:
            items.append(
                {
                    "kind": "human",
                    "title": "Direct Consensus",
                    "summary": (
                        f"{total_votes} vote{'s' if total_votes != 1 else ''}: "
                        f"{vote_real} real / {vote_edited} edited / {vote_ai} AI."
                    ),
                    "details": [],
                }
            )

    synthid_reports = getattr(current_image, "synthid_reports", None) or []
    if synthid_reports:
        detected = sum(1 for report in synthid_reports if report.result == "detected")
        not_detected = sum(
            1 for report in synthid_reports if report.result == "not_detected"
        )
        total_reports = detected + not_detected
        if total_reports > 0:
            items.append(
                {
                    "kind": "synthid",
                    "title": "Direct SynthID Reports",
                    "summary": (
                        f"{total_reports} report{'s' if total_reports != 1 else ''}: "
                        f"{detected} detected / {not_detected} not detected."
                    ),
                    "details": [],
                }
            )

    exif_row = _find_analyzer_row(analyzer_rows, "exif")
    if exif_row and str(exif_row.get("status", "")).upper() == "FOUND":
        findings = (exif_row.get("data") or {}).get("findings") or []
        finding_count = len(findings) if isinstance(findings, list) else 0
        items.append(
            {
                "kind": "exif",
                "title": "Direct AI Metadata",
                "summary": (
                    f"{finding_count} metadata finding{'s' if finding_count != 1 else ''}."
                ),
                "details": [],
            }
        )

    return items


def _build_distant_matches(context: AnalysisContext) -> list[dict[str, Any]]:
    base_phash, base_whash = compute_base_hashes(context.phash, context.whash)
    matches: list[dict[str, Any]] = []

    for neighbor in context.neighbors:
        neighbor_id = getattr(neighbor, "id", None)
        if neighbor_id == context.registry_id:
            continue

        c2pa_facts = _extract_c2pa_facts(neighbor)
        vote_counts = _extract_vote_counts(neighbor)
        synthid_counts = _extract_synthid_counts(neighbor)

        carried_type_count = sum(
            1
            for value in (c2pa_facts, vote_counts, synthid_counts)
            if value is not None and value != []
        )
        if carried_type_count == 0:
            continue

        phash = getattr(neighbor, "phash", None)
        whash = getattr(neighbor, "whash", None)
        (
            phash_distance,
            whash_distance,
            display_hash,
            display_label,
            display_distance,
        ) = compute_neighbor_distances(base_phash, base_whash, phash, whash)

        method = str(getattr(neighbor, "match_method", "hash") or "hash").lower()
        local_match = getattr(neighbor, "local_match", None)
        local_payload = None
        if local_match is not None:
            local_payload = {
                "extractor": getattr(local_match, "extractor", "orb"),
                "good_matches": int(getattr(local_match, "good_matches", 0) or 0),
                "inliers": int(getattr(local_match, "inliers", 0) or 0),
                "inlier_ratio": float(getattr(local_match, "inlier_ratio", 0.0) or 0.0),
                "crop_box": getattr(local_match, "crop_box", None),
            }

        distance = display_distance
        if method == "local":
            distance = None

        created_at = getattr(neighbor, "created_at", None)
        matches.append(
            {
                "image_id": neighbor_id,
                "phash": phash,
                "whash": whash,
                "hash_display": f"{display_hash} ({display_label})"
                if display_hash
                else phash,
                "distance": distance,
                "distance_phash": phash_distance,
                "distance_whash": whash_distance,
                "match_method": method,
                "match_method_label": _MATCH_METHOD_LABEL.get(method, "Hash"),
                "local": local_payload,
                "c2pa_facts": c2pa_facts,
                "votes": vote_counts,
                "synthid": synthid_counts,
                "sources": extract_sources(neighbor),
                "created_at": _iso_datetime(created_at),
                "_sort_method": _MATCH_METHOD_PRIORITY.get(method, 99),
                "_sort_types": carried_type_count,
                "_sort_time": _datetime_sort_key(created_at),
            }
        )

    matches.sort(
        key=lambda item: (
            item["_sort_method"],
            -item["_sort_types"],
            -item["_sort_time"],
        )
    )
    for item in matches:
        item.pop("_sort_method", None)
        item.pop("_sort_types", None)
        item.pop("_sort_time", None)
    return matches


def _extract_c2pa_facts(neighbor) -> list[str]:
    facts = []
    for fact in getattr(neighbor, "facts", []) or []:
        if getattr(fact, "analyzer", None) == "c2pa":
            facts.append(str(getattr(fact, "data", "")))
    return facts


def _extract_vote_counts(neighbor) -> dict[str, int] | None:
    consensus = getattr(neighbor, "consensus", None)
    if consensus is None:
        return None
    vote_real = int(getattr(consensus, "vote_real", 0) or 0)
    vote_edited = int(getattr(consensus, "vote_edited", 0) or 0)
    vote_ai = int(getattr(consensus, "vote_ai", 0) or 0)
    total_votes = vote_real + vote_edited + vote_ai
    if total_votes <= 0:
        return None
    return {
        "vote_real": vote_real,
        "vote_edited": vote_edited,
        "vote_ai": vote_ai,
        "total_votes": total_votes,
    }


def _extract_synthid_counts(neighbor) -> dict[str, int] | None:
    synthid = getattr(neighbor, "synthid", None)
    if synthid is None:
        return None
    detected = int(getattr(synthid, "detected", 0) or 0)
    not_detected = int(getattr(synthid, "not_detected", 0) or 0)
    total = detected + not_detected
    if total <= 0:
        return None
    return {"detected": detected, "not_detected": not_detected, "total": total}


def _find_analyzer_row(
    analyzer_rows: list[dict[str, Any]] | None,
    slug: str,
) -> dict[str, Any] | None:
    if not analyzer_rows:
        return None
    for row in analyzer_rows:
        if row.get("slug") == slug:
            return row
    return None


def _datetime_sort_key(value: Any) -> float:
    if isinstance(value, datetime):
        return value.timestamp()
    return 0.0


def _iso_datetime(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return ""

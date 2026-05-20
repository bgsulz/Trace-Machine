from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import joinedload

from ..analyzers.context import AnalysisContext
from ..analyzers.hash_utils import (
    iter_neighbor_views,
)
from ..models import ImageRegistry


_MATCH_METHOD_PRIORITY = {"hybrid": 0, "local": 1, "hash": 2}


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
            by_detector = _summarize_synthid_reports_by_detector(synthid_reports)
            items.append(
                {
                    "kind": "synthid",
                    "title": "Direct SynthID Reports",
                    "summary": (
                        f"{total_reports} report{'s' if total_reports != 1 else ''}: "
                        f"{detected} detected / {not_detected} not detected."
                    ),
                    "details": _format_synthid_detector_details(by_detector),
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
    matches: list[dict[str, Any]] = []

    for neighbor_view in iter_neighbor_views(context, include_self=False, include_local_payload=True):
        neighbor = neighbor_view["neighbor"]
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

        method = str(neighbor_view["match_method"] or "hash").lower()

        distance = neighbor_view["display_distance"]
        if method == "local":
            distance = None

        created_at = neighbor_view["created_at"]
        matches.append(
            {
                "image_id": neighbor_view["id"],
                "phash": neighbor_view["phash"],
                "whash": neighbor_view["whash"],
                "hash_display": neighbor_view["hash_display"],
                "distance": distance,
                "distance_phash": neighbor_view["phash_distance"],
                "distance_whash": neighbor_view["whash_distance"],
                "match_method": method,
                "match_method_label": neighbor_view["match_method_label"],
                "local": neighbor_view["local"],
                "c2pa_facts": c2pa_facts,
                "votes": vote_counts,
                "synthid": synthid_counts,
                "sources": neighbor_view["sources"],
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
    return {
        "detected": detected,
        "not_detected": not_detected,
        "total": total,
        "by_detector": getattr(synthid, "by_detector", {}) or {},
    }


def _summarize_synthid_reports_by_detector(reports) -> dict[str, dict[str, int]]:
    by_detector: dict[str, dict[str, int]] = {}
    for report in reports:
        detector = str(getattr(report, "detector", "") or "google_about_this_image")
        row = by_detector.setdefault(detector, {"detected": 0, "not_detected": 0})
        if getattr(report, "result", None) == "detected":
            row["detected"] += 1
        elif getattr(report, "result", None) == "not_detected":
            row["not_detected"] += 1
    return by_detector


def _format_synthid_detector_details(
    by_detector: dict[str, dict[str, int]]
) -> list[str]:
    labels = {
        "google_about_this_image": "Google",
        "openai_verify": "OpenAI",
    }
    details = []
    for detector, counts in by_detector.items():
        detected = int(counts.get("detected") or 0)
        not_detected = int(counts.get("not_detected") or 0)
        total = detected + not_detected
        if total <= 0:
            continue
        label = labels.get(detector, detector.replace("_", " "))
        details.append(f"{label}: {detected} detected / {not_detected} not detected")
    return details


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

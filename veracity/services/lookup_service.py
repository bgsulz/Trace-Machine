from __future__ import annotations

from typing import Any
from sqlalchemy.orm import joinedload

from ..dethumbnail import get_full_res_url
from ..models import ImageConsensus, ImageRegistry, ImageSource


def lookup_urls(urls: list[str]) -> dict[str, dict[str, Any]]:
    """Look up provenance data for a batch of image URLs.

    For each input URL, a dethumbnailed variant is also checked.
    Returns a dict keyed by original input URL. Each entry contains:
    - match_count: number of matched registry images
    - matches: all matched registry images (deterministically sorted)
    """
    # Preserve input order while dropping duplicates.
    seen_inputs: set[str] = set()
    unique_inputs: list[str] = []
    for url in urls:
        if url in seen_inputs:
            continue
        seen_inputs.add(url)
        unique_inputs.append(url)

    # Build mapping: candidate_url -> original_input_urls
    candidate_to_inputs: dict[str, set[str]] = {}
    for url in unique_inputs:
        candidate_to_inputs.setdefault(url, set()).add(url)
        full_res = get_full_res_url(url)
        if full_res:
            candidate_to_inputs.setdefault(full_res, set()).add(url)

    results: dict[str, dict[str, Any]] = {
        url: {"match_count": 0, "matches": []}
        for url in unique_inputs
    }
    if not unique_inputs or not candidate_to_inputs:
        return results

    # Single query: fetch all matching ImageSource rows with eager-loaded relations
    matched_sources = (
        ImageSource.query
        .filter(ImageSource.url.in_(list(candidate_to_inputs.keys())))
        .options(
            joinedload(ImageSource.image)
            .joinedload(ImageRegistry.consensus),
            joinedload(ImageSource.image)
            .joinedload(ImageRegistry.facts),
            joinedload(ImageSource.image)
            .joinedload(ImageRegistry.synthid_reports),
        )
        .all()
    )

    per_input_registry: dict[str, dict[int, dict[str, Any]]] = {
        url: {}
        for url in unique_inputs
    }

    for source in matched_sources:
        input_urls = candidate_to_inputs.get(source.url)
        if not input_urls:
            continue

        registry: ImageRegistry | None = source.image
        if registry is None:
            continue

        payload = _build_match_payload(registry)
        for input_url in input_urls:
            existing = per_input_registry[input_url].get(registry.id)
            if existing is None:
                row = payload.copy()
                row["matched_source_urls"] = [source.url]
                per_input_registry[input_url][registry.id] = row
                continue
            matched_urls = existing.get("matched_source_urls") or []
            if source.url not in matched_urls:
                matched_urls.append(source.url)
                existing["matched_source_urls"] = matched_urls

    for input_url in unique_inputs:
        matches = list(per_input_registry[input_url].values())
        for match in matches:
            match["matched_source_urls"] = sorted(match["matched_source_urls"])
        matches.sort(
            key=lambda item: (
                item.get("created_at") or "",
                int(item.get("registry_id") or 0),
            ),
            reverse=True,
        )
        results[input_url] = {
            "match_count": len(matches),
            "matches": matches,
        }

    return results


def _build_match_payload(registry: ImageRegistry) -> dict[str, Any]:
    consensus: ImageConsensus | None = registry.consensus
    vote_real = int(consensus.vote_real or 0) if consensus else 0
    vote_edited = int(consensus.vote_edited or 0) if consensus else 0
    vote_ai = int(consensus.vote_ai or 0) if consensus else 0
    total_votes = vote_real + vote_edited + vote_ai

    verdict = None
    if total_votes > 0:
        counts = {"real": vote_real, "edited": vote_edited, "ai": vote_ai}
        verdict = max(counts, key=counts.get)

    has_c2pa = any(fact.analyzer == "c2pa" for fact in (registry.facts or []))

    synthid = None
    reports = registry.synthid_reports or []
    if reports:
        detected = sum(
            1
            for report in reports
            if report.result == "detected"
        )
        not_detected = sum(
            1
            for report in reports
            if report.result == "not_detected"
        )
        if detected > not_detected:
            synthid = True
        elif not_detected > detected:
            synthid = False

    created_at = getattr(registry, "created_at", None)
    return {
        "registry_id": registry.id,
        "phash": registry.phash,
        "whash": registry.whash,
        "created_at": created_at.isoformat() if created_at else None,
        "vote_real": vote_real,
        "vote_edited": vote_edited,
        "vote_ai": vote_ai,
        "total_votes": total_votes,
        "verdict": verdict,
        "c2pa": has_c2pa,
        "synthid": synthid,
    }

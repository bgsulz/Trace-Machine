from __future__ import annotations

from sqlalchemy.orm import joinedload

from .dethumbnail import get_full_res_url
from .models import ImageConsensus, ImageRegistry, ImageSource, ProvenanceFact, SynthIDReport


def lookup_urls(urls: list[str]) -> dict[str, dict]:
    """Look up provenance data for a batch of image URLs.

    For each input URL a dethumbnailed variant is also checked.  Returns a dict
    keyed by the *original* input URL with provenance data for every match found
    in the database.
    """

    # Build mapping: candidate_url -> original_input_urls
    candidate_to_inputs: dict[str, set[str]] = {}
    for url in urls:
        candidate_to_inputs.setdefault(url, set()).add(url)
        full_res = get_full_res_url(url)
        if full_res:
            candidate_to_inputs.setdefault(full_res, set()).add(url)

    if not candidate_to_inputs:
        return {}

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

    results: dict[str, dict] = {}

    for source in matched_sources:
        input_urls = candidate_to_inputs.get(source.url)
        if not input_urls:
            continue

        registry: ImageRegistry = source.image
        consensus: ImageConsensus | None = registry.consensus

        vote_real = int(consensus.vote_real or 0) if consensus else 0
        vote_edited = int(consensus.vote_edited or 0) if consensus else 0
        vote_ai = int(consensus.vote_ai or 0) if consensus else 0
        total_votes = vote_real + vote_edited + vote_ai

        verdict = None
        if total_votes > 0:
            counts = {"real": vote_real, "edited": vote_edited, "ai": vote_ai}
            verdict = max(counts, key=counts.get)

        has_c2pa = any(f.analyzer == "c2pa" for f in registry.facts)

        synthid = None
        reports = registry.synthid_reports or []
        if reports:
            detected = sum(1 for r in reports if r.result == "detected")
            not_detected = sum(1 for r in reports if r.result == "not_detected")
            if detected > not_detected:
                synthid = True
            elif not_detected > detected:
                synthid = False

        payload = {
            "phash": registry.phash,
            "vote_real": vote_real,
            "vote_edited": vote_edited,
            "vote_ai": vote_ai,
            "total_votes": total_votes,
            "verdict": verdict,
            "c2pa": has_c2pa,
            "synthid": synthid,
        }
        for input_url in input_urls:
            if input_url in results:
                continue
            results[input_url] = payload.copy()

    return results

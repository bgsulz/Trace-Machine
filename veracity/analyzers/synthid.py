from __future__ import annotations
import json
import logging
import os
import requests
from flask import url_for

from .. import db
from ..models import ProvenanceFact
from .context import AnalysisContext
from .hash_utils import (
    compute_base_hashes,
    compute_neighbor_distances,
    extract_sources,
)

logger = logging.getLogger(__name__)

# Mock response for local development to save credits
MOCK_SERP_RESPONSE = True


def get_synthid_status(context: AnalysisContext) -> dict[str, object]:
    logger.info("Getting SynthID status for %s", context.phash)

    """
    Step 1: The 'Cheap' Check.
    Checks if we already have a record. If yes, return it.
    If no, return a 'WAITING' status that prompts the UI to show a button.
    """
    existing_fact = ProvenanceFact.query.filter_by(
        image_id=context.registry_id, analyzer="synthid"
    ).first()

    if existing_fact:
        data = json.loads(existing_fact.data)
        matches = _find_neighbor_matches(context)
        prev_matches = data.get("matches")
        data["matches"] = matches
        if prev_matches != matches:
            existing_fact.data = json.dumps(data)
            db.session.add(existing_fact)
            db.session.commit()
        return {
            "status": "FOUND" if data.get("detected") else "NOT FOUND",
            "summary": data.get("summary"),
            "data": data,
        }

    matches = _find_neighbor_matches(context)

    return {
        "status": "WAITING",
        "summary": "Manual check required.",
        "data": {
            "matches": matches,
        },
    }


def execute_synthid_search(
    analysis_id: str, context: AnalysisContext
) -> dict[str, object]:
    """
    Step 2: The 'Expensive' Execution.
    Called only when the user clicks the button.
    """
    # Double check DB to prevent race conditions saving double credits
    existing = ProvenanceFact.query.filter_by(
        image_id=context.registry_id, analyzer="synthid"
    ).first()
    if existing:
        return get_synthid_status(context)

    public_img_url = url_for(
        "main.serve_analysis_image", analysis_id=analysis_id, _external=True
    )
    logger.info("Public image URL: %s", public_img_url)

    if "127.0.0.1" in public_img_url or "localhost" in public_img_url:
        if not MOCK_SERP_RESPONSE:
            return {
                "status": "ERROR",
                "summary": "Cannot run SerpApi on localhost (tunnel required).",
                "data": {},
            }
        logger.info("Mocking SerpApi response for localhost")
        detected = True
        badge_text = "Mocked: Made with Google AI"
    else:
        # Real API Call
        api_key = os.environ.get("SERPAPI_KEY")
        if not api_key:
            return {
                "status": "ERROR",
                "summary": "Server missing SERPAPI_KEY",
                "data": {},
            }

        params = {
            "engine": "google_lens",
            "url": public_img_url,
            "api_key": api_key,
            "no_cache": "true",  # Optional, helps with debugging
        }

        try:
            resp = requests.get("https://serpapi.com/search", params=params, timeout=20)
            resp.raise_for_status()
            results = resp.json()
        except Exception:
            logger.exception("SerpApi failure")
            return {"status": "ERROR", "summary": "External API failed", "data": {}}

        detected = False
        badge_text = ""

        about = results.get("about_this_image", {})
        if (
            "google_ai_generated" in str(about).lower()
            or "made with google ai" in str(about).lower()
        ):
            detected = True
            badge_text = "Made with Google AI"

    summary = _format_detection_message(
        detected,
        badge_text,
        detected_fallback="SynthID detected",
        clean_fallback="No SynthID badge detected via Google Lens.",
    )

    fact_data = {
        "detected": detected,
        "badge_text": badge_text,
        "summary": summary,
        "matches": _find_neighbor_matches(context),  # Refresh neighbors
    }

    new_fact = ProvenanceFact(
        image_id=context.registry_id, analyzer="synthid", data=json.dumps(fact_data)
    )
    db.session.add(new_fact)
    db.session.commit()

    return {
        "status": "FOUND" if detected else "NOT FOUND",
        "summary": summary,
        "data": fact_data,
    }


def _find_neighbor_matches(context: AnalysisContext):
    """Reuse the neighbor logic to find if similar images have SynthID."""
    matches = []
    base_phash, base_whash = compute_base_hashes(context.phash, context.whash)

    for neighbor in context.neighbors:
        phash = getattr(neighbor, "phash", None)
        if not phash:
            continue

        neighbor_whash_val = getattr(neighbor, "whash", None)
        (
            phash_distance,
            whash_distance,
            display_hash,
            display_label,
            display_distance,
        ) = compute_neighbor_distances(
            base_phash, base_whash, phash, neighbor_whash_val
        )

        sources = extract_sources(neighbor)

        for fact in getattr(neighbor, "facts", []) or []:
            if fact.analyzer != "synthid":
                continue
            fact_json = json.loads(fact.data)
            detected = bool(fact_json.get("detected"))
            badge_text = fact_json.get("badge_text") or ""
            summary = fact_json.get("summary") or ""

            result_text = _format_detection_message(detected, badge_text)

            matches.append(
                {
                    "phash": phash,
                    "whash": neighbor_whash_val,
                    "hash_display": f"{display_hash} ({display_label})",
                    "distance": display_distance,
                    "distance_phash": phash_distance,
                    "distance_whash": whash_distance,
                    "detected": detected,
                    "badge": badge_text,
                    "summary": summary,
                    "result_text": result_text,
                    "sources": sources,
                }
            )
            break

    logger.info("SynthID neighbor facts found: %d", len(matches))
    return matches


def _format_detection_message(
    detected: bool,
    badge_text: str,
    *,
    detected_fallback: str = "SynthID detected",
    clean_fallback: str = "No SynthID detected",
) -> str:
    if detected:
        return badge_text or detected_fallback
    if badge_text:
        return badge_text
    return clean_fallback

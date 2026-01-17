from __future__ import annotations
import json
import logging
import os
from pathlib import Path

import requests
from flask import current_app, url_for
from lxml import html as lxml_html

from .. import db
from ..models import ProvenanceFact
from .context import AnalysisContext
from .hash_utils import (
    compute_base_hashes,
    compute_neighbor_distances,
    extract_sources,
)

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SERP_MOCK_PATH = _PROJECT_ROOT / "static" / ".local" / "serp_mock.html"


def _mock_mode_enabled() -> bool:
    try:
        config_value = current_app.config.get("SYNTHID_MOCK_MODE")
    except RuntimeError:
        config_value = None

    if config_value is not None:
        return config_value

    return os.environ.get("SYNTHID_MOCK_MODE") == "1"


def get_synthid_status(context: AnalysisContext) -> dict[str, object]:
    logger.info("Getting SynthID status for %s", context.phash)

    existing_fact = ProvenanceFact.query.filter_by(
        image_id=context.registry_id, analyzer="synthid"
    ).first()

    matches = _find_neighbor_matches(context)

    if existing_fact:
        data = json.loads(existing_fact.data)
        response_data = dict(data)
        response_data["matches"] = matches
        return {
            "status": "FOUND" if response_data.get("detected") else "NOT FOUND",
            "summary": response_data.get("summary"),
            "data": response_data,
        }

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
    # Double check DB to prevent race conditions saving double credits
    existing = ProvenanceFact.query.filter_by(
        image_id=context.registry_id, analyzer="synthid"
    ).first()
    if existing:
        return get_synthid_status(context)

    public_img_url = url_for(
        "main.serve_analysis_image", analysis_id=analysis_id, _external=True
    )
    logger.debug("Public image URL: %s", public_img_url)

    mock_mode = _mock_mode_enabled()
    if mock_mode:
        logger.info("Mocking SerpApi response (SYNTHID_MOCK_MODE enabled)")
        detected, badge_text = _parse_mock_serp_html()
        if detected is None:
            return {
                "status": "ERROR",
                "summary": "Local Serp mock missing Made with Google AI badge.",
                "data": {},
            }
    else:
        if _is_local_url(public_img_url):
            return {
                "status": "ERROR",
                "summary": "Cannot run SerpApi on localhost (tunnel required).",
                "data": {},
            }
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

        raw_html_url = results.get("search_metadata", {}).get("raw_html_file")
        if not raw_html_url:
            logger.error("SerpApi response missing raw_html_file; falling back to JSON")
        else:
            try:
                html_resp = requests.get(raw_html_url, timeout=20)
                html_resp.raise_for_status()
                detected, badge_text = _parse_html_for_badge(html_resp.content)
            except Exception:
                logger.exception("Failed to download SerpApi raw HTML")
                return {
                    "status": "ERROR",
                    "summary": "External API failed",
                    "data": {},
                }

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
    }

    new_fact = ProvenanceFact(
        image_id=context.registry_id, analyzer="synthid", data=json.dumps(fact_data)
    )
    db.session.add(new_fact)
    db.session.commit()

    matches = _find_neighbor_matches(context, use_cache=False)
    response_data = dict(fact_data)
    response_data["matches"] = matches

    return {
        "status": "FOUND" if detected else "NOT FOUND",
        "summary": summary,
        "data": response_data,
    }


def _find_neighbor_matches(context: AnalysisContext, *, use_cache: bool = True):
    if use_cache:
        cached = getattr(context, "_synthid_neighbor_matches", None)
        if cached is not None:
            return cached

    matches = []
    base_phash, base_whash = _get_base_hashes(context)

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

    if use_cache:
        context._synthid_neighbor_matches = matches
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


def _get_base_hashes(context: AnalysisContext):
    cached = getattr(context, "_base_hashes", None)
    if cached is None:
        cached = compute_base_hashes(context.phash, context.whash)
        setattr(context, "_base_hashes", cached)
    return cached


def _is_local_url(url: str) -> bool:
    if not url:
        return False
    return any(x in url for x in ("127.0.0.1", "localhost", "::1", "[::1]"))


def _parse_mock_serp_html() -> tuple[bool | None, str]:
    if not _SERP_MOCK_PATH.exists():
        logger.error("Serp mock HTML not found at %s", _SERP_MOCK_PATH)
        return None, ""

    try:
        html_content = _SERP_MOCK_PATH.read_bytes()
    except OSError:
        logger.exception("Unable to read Serp mock HTML at %s", _SERP_MOCK_PATH)
        return None, ""

    return _parse_html_for_badge(html_content)


def _parse_html_for_badge(html_content: bytes | str) -> tuple[bool, str]:
    if not html_content:
        return False, ""

    if isinstance(html_content, bytes):
        decoded = html_content.decode("utf-8", errors="ignore")
    else:
        decoded = html_content

    try:
        tree = lxml_html.fromstring(decoded)
    except Exception:
        logger.warning("Failed to parse HTML tree for SynthID badge", exc_info=True)
        return _search_badge_in_text(decoded)

    specific_xpath = (
        "/html/body/c-wiz/div/c-wiz/div/div[1]/div[1]/section/div[2]"
        "/g-accordion/div/g-accordion-expander/div[1]/div[2]/div/div"
    )
    text_xpath = (
        "//*[contains(translate(text(), "
        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), "
        "'made with google ai')]"
    )

    for xpath in (specific_xpath, text_xpath):
        try:
            elements = tree.xpath(xpath)
        except Exception:
            continue
        for el in elements:
            text = (el.text_content() or "").strip()
            if text and "made with google ai" in text.lower():
                return True, "Made with Google AI"

    return _search_badge_in_text(decoded)


def _search_badge_in_text(text: str) -> tuple[bool, str]:
    if "made with google ai" in text.lower():
        return True, "Made with Google AI"
    return False, ""

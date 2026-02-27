from __future__ import annotations

import base64
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import current_app

from ..analysis_cache import store_analysis_payload, store_cached_analyzer_row
from .analysis_service import _format_public_url
from ..analyzers.manager import ANALYZERS, run_all_analyzers
from .remote_image_service import fetch_remote_image
from ..registry import prepare_analysis_context
from . import voting_service

logger = logging.getLogger(__name__)

BATCH_ANALYZERS = tuple(spec for spec in ANALYZERS if spec.slug != "tineye")
MAX_BATCH_URLS = 10


def process_batch_urls(urls: list[str]) -> list[dict]:
    """Process a list of image URLs in parallel.

    Returns a list of result dicts, one per unique URL, each containing:
    - url, analysis_id, error, image_data_url, public_url_display
    """
    seen: set[str] = set()
    unique_urls: list[str] = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            unique_urls.append(url)

    app = current_app._get_current_object()
    results: dict[str, dict] = {}

    with ThreadPoolExecutor(max_workers=5, thread_name_prefix="batch") as executor:
        future_to_url = {
            executor.submit(_process_single_url, app, url): url
            for url in unique_urls
        }
        for future in as_completed(future_to_url):
            url = future_to_url[future]
            try:
                results[url] = future.result()
            except Exception as exc:
                logger.exception("Batch processing failed for %s", url)
                results[url] = {
                    "url": url,
                    "analysis_id": None,
                    "error": str(exc) or "Unexpected error",
                    "image_data_url": None,
                    "public_url_display": _format_public_url(url),
                }

    return [results[url] for url in unique_urls]


def _process_single_url(app, url: str) -> dict:
    """Process a single URL within a Flask app context."""
    with app.app_context():
        return _do_process_url(url)


def _do_process_url(url: str) -> dict:
    """Fetch, analyze, and cache a single image URL."""
    fetched = fetch_remote_image(url)
    image_bytes = fetched.image_bytes
    mime_type = fetched.mime_type

    context = prepare_analysis_context(image_bytes)
    voting_service.persist_source_url(context.phash, url)

    public_url_display = _format_public_url(url)

    metadata = {
        "mime_type": mime_type,
        "source": "url",
        "image_url": fetched.fetch_url,
        "public_url": url,
        "analysis_link": None,
        "phash": context.phash,
        "whash": context.whash,
        "registry_id": context.registry_id,
        "crop_box": None,
        "full_res_url": fetched.full_res_url if not fetched.upgraded else None,
        "image_width": context.width,
        "image_height": context.height,
        "public_url_display": public_url_display,
    }
    analysis_id = store_analysis_payload(None, image_bytes, metadata)

    # Prime analyzer cache (excluding TinEye)
    rows = run_all_analyzers(context, analyzers=BATCH_ANALYZERS)
    for row in rows:
        slug = row.get("slug")
        if slug:
            store_cached_analyzer_row(analysis_id, slug, row)

    # Build data URL for thumbnail
    image_b64 = base64.b64encode(image_bytes).decode("ascii")
    image_data_url = f"data:{mime_type};base64,{image_b64}"

    return {
        "url": url,
        "analysis_id": analysis_id,
        "error": None,
        "image_data_url": image_data_url,
        "public_url_display": public_url_display,
    }

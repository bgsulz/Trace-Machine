from __future__ import annotations
import fnmatch
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, UTC
from typing import Protocol, TypedDict

import requests

from .. import db
from ..models import TinEyeResult
from .context import AnalysisContext
from .hash_utils import compute_base_hashes, compute_neighbor_distances, extract_sources

logger = logging.getLogger(__name__)

STALE_THRESHOLD_DAYS = 60

TINEYE_API_URL = "https://api.tineye.com/rest/search/"
SIMILARITY_THRESHOLD = 70 # Minimum score (0-100) to include in results

SHAME_LIST_URL = (
    "https://raw.githubusercontent.com/laylavish/uBlockOrigin-HUGE-AI-Blocklist"
    "/main/list_uBlacklist.txt"
)
SHAME_LIST_TTL_SECONDS = 3600  # 1 hour


class Matcher(Protocol):
    def matches(self, url: str) -> bool: ...


@dataclass(frozen=True)
class GlobMatcher:
    pattern: str

    def matches(self, url: str) -> bool:
        return fnmatch.fnmatch(url, self.pattern)


@dataclass(frozen=True)
class RegexMatcher:
    pattern: re.Pattern[str]

    def matches(self, url: str) -> bool:
        return self.pattern.search(url) is not None


_cached_matchers: list[Matcher] | None = None
_cached_at: float = 0.0


def _fetch_shame_list_raw() -> str:
    try:
        resp = requests.get(SHAME_LIST_URL, timeout=30)
        resp.raise_for_status()
        return resp.text
    except Exception:
        logger.exception("Failed to fetch shame list from %s", SHAME_LIST_URL)
        return ""


def _parse_shame_list(raw_text: str) -> list[Matcher]:
    matchers: list[Matcher] = []

    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue

        if line.startswith("#"):
            continue

        if line.startswith("*"):
            matchers.append(GlobMatcher(pattern=line))
        elif line.startswith("/"):
            regex_str, flags = _parse_regex_line(line)
            if regex_str:
                try:
                    compiled = re.compile(regex_str, flags)
                    matchers.append(RegexMatcher(pattern=compiled))
                except re.error:
                    logger.warning("Invalid regex in shame list: %s", line)

    return matchers


def _parse_regex_line(line: str) -> tuple[str, int]:
    if not line.startswith("/"):
        return "", 0

    flags = 0
    if line.endswith("/i"):
        flags = re.IGNORECASE
        line = line[:-2]
    elif line.endswith("/"):
        line = line[:-1]

    regex_str = line[1:]
    return regex_str, flags


def get_shame_list_matchers(*, force_refresh: bool = False) -> list[Matcher]:
    global _cached_matchers, _cached_at

    now = time.time()
    if not force_refresh and _cached_matchers is not None:
        if (now - _cached_at) < SHAME_LIST_TTL_SECONDS:
            return _cached_matchers

    raw_text = _fetch_shame_list_raw()
    if not raw_text:
        if _cached_matchers is not None:
            return _cached_matchers
        return []

    _cached_matchers = _parse_shame_list(raw_text)
    _cached_at = now
    logger.info("Loaded %d shame list matchers", len(_cached_matchers))
    return _cached_matchers


def url_matches_shame_list(url: str, matchers: list[Matcher] | None = None) -> bool:
    if matchers is None:
        matchers = get_shame_list_matchers()

    for matcher in matchers:
        if matcher.matches(url):
            return True
    return False


def clear_shame_list_cache() -> None:
    global _cached_matchers, _cached_at
    _cached_matchers = None
    _cached_at = 0.0


class TinEyeMatch(TypedDict):
    url: str
    domain: str
    crawl_date: str  # ISO format
    similarity: float  # 0.0 to 1.0


class TinEyeAPIResult(TypedDict):
    success: bool
    error: str | None
    total_matches: int
    matches: list[TinEyeMatch]


class BucketedMatches(TypedDict):
    oldest: list[TinEyeMatch]
    newest: list[TinEyeMatch]
    shame_list: list[TinEyeMatch]


class ProcessedTinEyeResult(TypedDict):
    success: bool
    error: str | None
    total_matches: int
    filtered_match_count: int
    earliest_date: str | None  # ISO format
    on_shame_list: bool
    buckets: BucketedMatches


def call_tineye_api(
    image_bytes: bytes | None = None,
    image_url: str | None = None,
) -> TinEyeAPIResult:
    api_key = os.environ.get("TINEYE_KEY", "")
    logger.info("TinEye API call - URL: %s, API key present: %s", image_url, bool(api_key))
    
    if not api_key:
        return {
            "success": False,
            "error": "Server missing TINEYE_KEY",
            "total_matches": 0,
            "matches": [],
        }

    headers = {"x-api-key": api_key}
    
    try:
        if image_url:
            params = {
                "image_url": image_url,
                "limit": 100,
                "sort": "crawl_date",
                "order": "asc",
            }
            logger.info("Making TinEye API request to %s with params: %s", TINEYE_API_URL, params)
            resp = requests.get(
                TINEYE_API_URL, params=params, headers=headers, timeout=30
            )
            logger.info("TinEye API response status: %s", resp.status_code)
        else:
            return {
                "success": False,
                "error": "No image URL provided",
                "total_matches": 0,
                "matches": [],
            }

        resp.raise_for_status()
        data = resp.json()
        logger.info("TinEye API raw response: %s", json.dumps(data, indent=2))

    except requests.RequestException as e:
        logger.exception("TinEye API request failed")
        return {
            "success": False,
            "error": f"API request failed: {e}",
            "total_matches": 0,
            "matches": [],
        }
    except ValueError:
        logger.exception("TinEye API returned invalid JSON")
        return {
            "success": False,
            "error": "Invalid response from API",
            "total_matches": 0,
            "matches": [],
        }

    if data.get("code") != 200:
        error_messages = data.get("messages", ["Unknown error"])
        logger.error("TinEye API returned error code %s: %s", data.get("code"), error_messages)
        return {
            "success": False,
            "error": "; ".join(error_messages),
            "total_matches": 0,
            "matches": [],
        }

    results = data.get("results", {})
    raw_matches = results.get("matches", [])
    total_results = results.get("total_results", 0)
    
    logger.info("TinEye API results - total_results: %s, raw_matches count: %s", total_results, len(raw_matches))

    matches: list[TinEyeMatch] = []
    for m in raw_matches:
        score = m.get("score", 0)
        backlinks = m.get("backlinks", [])
        
        crawl_date_str = ""
        url = ""
        if backlinks:
            first_backlink = backlinks[0]
            url = first_backlink.get("url", "") or first_backlink.get("backlink", "")
            raw_date = first_backlink.get("crawl_date", "")
            if raw_date:
                try:
                    parsed = datetime.strptime(raw_date, "%Y-%m-%d")
                    crawl_date_str = parsed.isoformat()
                except ValueError:
                    crawl_date_str = raw_date

        matches.append({
            "url": url,
            "domain": m.get("domain", ""),
            "crawl_date": crawl_date_str,
            "similarity": score / 100.0,
        })

    logger.info("Processed %s matches from TinEye API", len(matches))
    return {
        "success": True,
        "error": None,
        "total_matches": total_results,
        "matches": matches,
    }


def filter_matches_by_similarity(
    matches: list[TinEyeMatch],
    threshold: float = SIMILARITY_THRESHOLD / 100.0,
) -> list[TinEyeMatch]:
    filtered = [m for m in matches if m["similarity"] >= threshold]
    logger.info("Filtered matches: %s/%s above similarity threshold %.2f", len(filtered), len(matches), threshold)
    return filtered


def extract_earliest_date(matches: list[TinEyeMatch]) -> str | None:
    dates: list[str] = []
    for m in matches:
        if m["crawl_date"]:
            dates.append(m["crawl_date"])
    
    if not dates:
        logger.info("No crawl dates found in matches")
        return None
    
    dates.sort()
    earliest = dates[0]
    logger.info("Earliest crawl date found: %s", earliest)
    return earliest


def bucket_matches(
    matches: list[TinEyeMatch],
    matchers: list[Matcher] | None = None,
) -> BucketedMatches:
    if matchers is None:
        matchers = get_shame_list_matchers()

    sorted_by_date = sorted(
        [m for m in matches if m["crawl_date"]],
        key=lambda m: m["crawl_date"],
    )

    oldest = sorted_by_date[:5]
    newest = sorted_by_date[-5:][::-1] if len(sorted_by_date) > 5 else []

    shame_list: list[TinEyeMatch] = []
    for m in matches:
        if m["url"] and url_matches_shame_list(m["url"], matchers):
            shame_list.append(m)

    logger.info("Bucketed matches - oldest: %s, newest: %s, shame_list: %s", len(oldest), len(newest), len(shame_list))
    return {
        "oldest": oldest,
        "newest": newest,
        "shame_list": shame_list,
    }


def process_tineye_response(
    api_result: TinEyeAPIResult,
    matchers: list[Matcher] | None = None,
) -> ProcessedTinEyeResult:
    logger.info("Processing TinEye response - success: %s, total_matches: %s", api_result["success"], api_result["total_matches"])
    
    if not api_result["success"]:
        return {
            "success": False,
            "error": api_result["error"],
            "total_matches": 0,
            "filtered_match_count": 0,
            "earliest_date": None,
            "on_shame_list": False,
            "buckets": {"oldest": [], "newest": [], "shame_list": []},
        }

    filtered = filter_matches_by_similarity(api_result["matches"])
    earliest = extract_earliest_date(filtered)
    buckets = bucket_matches(filtered, matchers)

    result = {
        "success": True,
        "error": None,
        "total_matches": api_result["total_matches"],
        "filtered_match_count": len(filtered),
        "earliest_date": earliest,
        "on_shame_list": len(buckets["shame_list"]) > 0,
        "buckets": buckets,
    }
    
    logger.info("Processed TinEye result - total_matches: %s, filtered_matches: %s, earliest: %s, on_shame_list: %s", 
                result["total_matches"], len(filtered), earliest, result["on_shame_list"])
    return result


def _is_result_stale(result: TinEyeResult) -> bool:
    if not result.searched_at:
        return True
    threshold = datetime.now(UTC) - timedelta(days=STALE_THRESHOLD_DAYS)
    searched_at = result.searched_at
    if searched_at.tzinfo is None:
        searched_at = searched_at.replace(tzinfo=UTC)
    return searched_at < threshold


def _build_summary(
    total_matches: int,
    filtered_match_count: int,
    earliest_date: str | None,
    on_shame_list: bool,
) -> str:
    # For sandbox API, use filtered_match_count instead of total_matches
    display_count = filtered_match_count if filtered_match_count > 0 else total_matches
    
    if display_count == 0:
        return "No matches found."

    parts = [f"{display_count} matches found."]

    if earliest_date:
        try:
            dt = datetime.fromisoformat(earliest_date)
            parts.append(f"Earliest: {dt.strftime('%b %Y')}.")
        except ValueError:
            pass

    if on_shame_list:
        parts.append("⚠️ Found on AI image sites.")
    else:
        parts.append("Not on known AI sites.")

    return " ".join(parts)


def _find_neighbor_matches(context: AnalysisContext) -> list[dict]:
    matches = []
    base_phash, base_whash = compute_base_hashes(context.phash, context.whash)

    for neighbor in context.neighbors:
        neighbor_phash = getattr(neighbor, "phash", None)
        if not neighbor_phash:
            continue

        tineye_result = getattr(neighbor, "tineye_result", None)
        if not tineye_result:
            continue

        neighbor_whash = getattr(neighbor, "whash", None)
        (
            phash_distance,
            whash_distance,
            display_hash,
            display_label,
            display_distance,
        ) = compute_neighbor_distances(base_phash, base_whash, neighbor_phash, neighbor_whash)

        sources = extract_sources(neighbor)

        earliest_date = None
        if tineye_result.earliest_date:
            earliest_date = tineye_result.earliest_date.isoformat()

        # Calculate filtered match count from stored buckets
        filtered_match_count = 0
        try:
            buckets = json.loads(tineye_result.matches_json)
            filtered_match_count = len(buckets.get("oldest", [])) + len(buckets.get("newest", [])) + len(buckets.get("shame_list", []))
        except (json.JSONDecodeError, TypeError):
            pass

        matches.append({
            "phash": neighbor_phash,
            "whash": neighbor_whash,
            "hash_display": f"{display_hash} ({display_label})",
            "distance": display_distance,
            "distance_phash": phash_distance,
            "distance_whash": whash_distance,
            "total_matches": tineye_result.total_matches,
            "earliest_date": earliest_date,
            "on_shame_list": tineye_result.on_shame_list,
            "summary": _build_summary(
                tineye_result.total_matches,
                filtered_match_count,
                earliest_date,
                tineye_result.on_shame_list,
            ),
            "sources": sources,
        })

    return matches


def get_tineye_status(context: AnalysisContext) -> dict[str, object]:
    logger.info("Getting TinEye status for %s", context.phash)

    existing = TinEyeResult.query.filter_by(image_id=context.registry_id).first()
    matches = _find_neighbor_matches(context)

    if existing:
        is_stale = _is_result_stale(existing)

        try:
            buckets = json.loads(existing.matches_json)
        except (json.JSONDecodeError, TypeError):
            buckets = {"oldest": [], "newest": [], "shame_list": []}

        earliest_date = None
        if existing.earliest_date:
            earliest_date = existing.earliest_date.isoformat()

        # Calculate filtered match count from stored buckets
        filtered_match_count = len(buckets.get("oldest", [])) + len(buckets.get("newest", [])) + len(buckets.get("shame_list", []))

        summary = _build_summary(
            existing.total_matches,
            filtered_match_count,
            earliest_date,
            existing.on_shame_list,
        )

        # Use filtered match count for status determination
        status = "STALE" if is_stale else ("FOUND" if filtered_match_count > 0 else "NOT FOUND")

        return {
            "status": status,
            "summary": summary,
            "data": {
                "total_matches": existing.total_matches,
                "earliest_date": earliest_date,
                "on_shame_list": existing.on_shame_list,
                "buckets": buckets,
                "searched_at": existing.searched_at.isoformat() if existing.searched_at else None,
                "matches": matches,
            },
        }

    return {
        "status": "WAITING",
        "summary": "Manual check required.",
        "data": {
            "matches": matches,
        },
    }


def execute_tineye_search(
    analysis_id: str,
    context: AnalysisContext,
) -> dict[str, object]:
    logger.info("Executing TinEye search for %s", context.phash)

    existing = TinEyeResult.query.filter_by(image_id=context.registry_id).first()
    if existing and not _is_result_stale(existing):
        logger.info("Using fresh existing TinEye result for %s", context.phash)
        return get_tineye_status(context)

    # Generate external URL for the image
    from flask import url_for
    image_url = url_for(
        "main.serve_analysis_image", analysis_id=analysis_id, _external=True
    )
    
    logger.info("Generated external URL for TinEye search: %s", image_url)
    api_result = call_tineye_api(image_url=image_url)

    if not api_result["success"]:
        logger.error("TinEye API call failed: %s", api_result["error"])
        return {
            "status": "ERROR",
            "summary": api_result["error"] or "Something went wrong.",
            "data": {
                "matches": _find_neighbor_matches(context),
            },
        }

    processed = process_tineye_response(api_result)
    logger.info("TinEye processing completed - total_matches: %s, success: %s", processed["total_matches"], processed["success"])

    earliest_dt = None
    if processed["earliest_date"]:
        try:
            earliest_dt = datetime.fromisoformat(processed["earliest_date"])
            if earliest_dt.tzinfo is None:
                earliest_dt = earliest_dt.replace(tzinfo=UTC)
        except ValueError:
            pass

    if existing:
        logger.info("Updating existing TinEye result for %s", context.phash)
        existing.total_matches = processed["total_matches"]
        existing.earliest_date = earliest_dt
        existing.on_shame_list = processed["on_shame_list"]
        existing.matches_json = json.dumps(processed["buckets"])
        existing.searched_at = datetime.now(UTC)
    else:
        logger.info("Creating new TinEye result for %s", context.phash)
        existing = TinEyeResult(
            image_id=context.registry_id,
            total_matches=processed["total_matches"],
            earliest_date=earliest_dt,
            on_shame_list=processed["on_shame_list"],
            matches_json=json.dumps(processed["buckets"]),
            searched_at=datetime.now(UTC),
        )
        db.session.add(existing)

    try:
        db.session.commit()
        logger.info("TinEye result saved to database")
    except Exception:
        db.session.rollback()
        logger.exception("Failed to save TinEye result")
        return {
            "status": "ERROR",
            "summary": "Failed to save results.",
            "data": {
                "matches": _find_neighbor_matches(context),
            },
        }

    return get_tineye_status(context)

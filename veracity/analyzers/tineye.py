from __future__ import annotations
import fnmatch
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, TypedDict

import requests

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


def call_tineye_api(image_url: str | None = None) -> TinEyeAPIResult:
    api_key = os.environ.get("TINEYE_KEY", "")
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
            resp = requests.get(
                TINEYE_API_URL, params=params, headers=headers, timeout=30
            )
        else:
            return {
                "success": False,
                "error": "No image URL provided",
                "total_matches": 0,
                "matches": [],
            }

        resp.raise_for_status()
        data = resp.json()

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
    return [m for m in matches if m["similarity"] >= threshold]


def extract_earliest_date(matches: list[TinEyeMatch]) -> str | None:
    dates = [m["crawl_date"] for m in matches if m["crawl_date"]]
    return min(dates) if dates else None


def _match_identity(match: TinEyeMatch) -> tuple[str, str, str]:
    return (
        match.get("url", "") or "",
        match.get("domain", "") or "",
        match.get("crawl_date", "") or "",
    )


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
    if len(sorted_by_date) <= 5:
        newest = list(reversed(sorted_by_date))
    else:
        newest_candidates = list(reversed(sorted_by_date[-5:]))
        oldest_identities = {_match_identity(match) for match in oldest}
        newest = []
        for match in newest_candidates:
            identity = _match_identity(match)
            if identity in oldest_identities:
                continue
            newest.append(match)

    shame_list: list[TinEyeMatch] = [
        m for m in matches if m["url"] and url_matches_shame_list(m["url"], matchers)
    ]

    return {
        "oldest": oldest,
        "newest": newest,
        "shame_list": shame_list,
    }


def _load_bucket_payload(raw_json: str | None) -> BucketedMatches:
    empty: BucketedMatches = {"oldest": [], "newest": [], "shame_list": []}
    if not raw_json:
        return empty

    try:
        data = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError):
        return empty

    return {
        "oldest": data.get("oldest", []) or [],
        "newest": data.get("newest", []) or [],
        "shame_list": data.get("shame_list", []) or [],
    }




def process_tineye_response(
    api_result: TinEyeAPIResult,
    matchers: list[Matcher] | None = None,
) -> ProcessedTinEyeResult:
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

    return {
        "success": True,
        "error": None,
        "total_matches": api_result["total_matches"],
        "filtered_match_count": len(filtered),
        "earliest_date": earliest,
        "on_shame_list": len(buckets["shame_list"]) > 0,
        "buckets": buckets,
    }


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

        buckets = _load_bucket_payload(tineye_result.matches_json)
        
        # Simple count of unique matches across all buckets
        seen = {
            _match_identity(match)
            for match in (
                list(buckets.get("oldest", []))
                + list(buckets.get("newest", []))
                + list(buckets.get("shame_list", []))
            )
        }
        filtered_match_count = len(seen)

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
    matches = _find_neighbor_matches(context)

    return {
        "status": "WAITING",
        "summary": "Manual check required.",
        "data": {
            "matches": matches,
            "allow_manual_refresh": True,
        },
    }



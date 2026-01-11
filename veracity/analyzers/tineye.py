from __future__ import annotations
import fnmatch
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, TypedDict

import requests

logger = logging.getLogger(__name__)

TINEYE_API_URL = "https://api.tineye.com/rest/search/"
SIMILARITY_THRESHOLD = 40  # Minimum score (0-100) to include in results

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
    earliest_date: str | None  # ISO format
    on_shame_list: bool
    buckets: BucketedMatches


def call_tineye_api(
    image_bytes: bytes | None = None,
    image_url: str | None = None,
) -> TinEyeAPIResult:
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
        elif image_bytes:
            params = {
                "limit": "100",
                "sort": "crawl_date",
                "order": "asc",
            }
            files = {"image_upload": ("image.jpg", image_bytes)}
            resp = requests.post(
                TINEYE_API_URL, data=params, files=files, headers=headers, timeout=30
            )
        else:
            return {
                "success": False,
                "error": "No image provided",
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
    dates: list[str] = []
    for m in matches:
        if m["crawl_date"]:
            dates.append(m["crawl_date"])
    
    if not dates:
        return None
    
    dates.sort()
    return dates[0]


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

    return {
        "oldest": oldest,
        "newest": newest,
        "shame_list": shame_list,
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
        "earliest_date": earliest,
        "on_shame_list": len(buckets["shame_list"]) > 0,
        "buckets": buckets,
    }

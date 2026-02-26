from __future__ import annotations
import fnmatch
import logging
import os
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol, TypedDict
from urllib.parse import urlparse

import requests
from flask import current_app

from .context import AnalysisContext

logger = logging.getLogger(__name__)

TINEYE_API_URL = "https://api.tineye.com/rest/search/"
SIMILARITY_THRESHOLD = 70 # Minimum score (0-100) to include in results

SHAME_LIST_URL = (
    "https://raw.githubusercontent.com/laylavish/uBlockOrigin-HUGE-AI-Blocklist"
    "/main/list_uBlacklist.txt"
)
SHAME_LIST_TTL_SECONDS = 86400  # 24 hours

_CACHE_FILENAME = "shame_list_cache.txt"
_SEED_PATH = Path(__file__).parent.parent / "static" / "shame_list_seed.txt"


class Matcher(Protocol):
    def matches(self, url: str) -> bool: ...


@dataclass(frozen=True)
class GlobMatcher:
    pattern: str

    def matches(self, url: str) -> bool:
        # Try the original pattern first
        if fnmatch.fnmatch(url, self.pattern):
            return True
        # Handle apex domains: *://*.example.com/* should also match https://example.com/
        # The pattern *://*.example.com/* won't match apex domains because * requires
        # at least one character. Generate an alternative pattern for apex domains.
        if "*://*." in self.pattern:
            apex_pattern = self.pattern.replace("*://*.", "*://", 1)
            if fnmatch.fnmatch(url, apex_pattern):
                return True
        return False


@dataclass(frozen=True)
class RegexMatcher:
    pattern: re.Pattern[str]

    def matches(self, url: str) -> bool:
        return self.pattern.search(url) is not None


_cached_matchers: list[Matcher] | None = None
_cached_at: float = 0.0
_cache_lock = threading.Lock()


def _get_cache_path() -> Path | None:
    """Get the path to the instance cache file, or None if unavailable."""
    try:
        instance_path = current_app.instance_path
        return Path(instance_path) / _CACHE_FILENAME
    except RuntimeError:
        # No Flask app context
        return None


def _fetch_shame_list_raw() -> str:
    """Fetch shame list from URL, with fallback to cached file and bundled seed."""
    # Try fetching from URL first
    try:
        resp = requests.get(SHAME_LIST_URL, timeout=30)
        resp.raise_for_status()
        raw_text = resp.text

        # On success, write to instance cache
        cache_path = _get_cache_path()
        if cache_path:
            try:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(raw_text, encoding="utf-8")
                logger.debug("Wrote shame list cache to %s", cache_path)
            except OSError:
                logger.warning("Failed to write shame list cache", exc_info=True)

        return raw_text
    except Exception:
        logger.warning("Failed to fetch shame list from %s, trying fallbacks", SHAME_LIST_URL)

    # Fallback 1: try instance cache
    cache_path = _get_cache_path()
    if cache_path and cache_path.exists():
        try:
            raw_text = cache_path.read_text(encoding="utf-8")
            logger.info("Loaded shame list from instance cache: %s", cache_path)
            return raw_text
        except OSError:
            logger.warning("Failed to read shame list cache", exc_info=True)

    # Fallback 2: bundled seed file
    if _SEED_PATH.exists():
        try:
            raw_text = _SEED_PATH.read_text(encoding="utf-8")
            logger.info("Loaded shame list from bundled seed: %s", _SEED_PATH)
            return raw_text
        except OSError:
            logger.warning("Failed to read bundled shame list seed", exc_info=True)

    logger.error("All shame list sources failed")
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

    with _cache_lock:
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
    with _cache_lock:
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
    intelligence: dict[str, object]


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


def _extract_domain(match: TinEyeMatch) -> str:
    domain = (match.get("domain") or "").strip().lower()
    if domain:
        return domain.removeprefix("www.")

    url = (match.get("url") or "").strip()
    if not url:
        return ""

    try:
        parsed = urlparse(url)
    except Exception:
        return ""

    host = (parsed.netloc or "").strip().lower()
    if host:
        return host.removeprefix("www.")
    return ""


def _classify_domain(domain: str) -> str:
    if not domain:
        return "other"

    domain = domain.lower()

    ai_hosts = (
        "civitai.com",
        "lexica.art",
        "openart.ai",
        "mage.space",
        "tensor.art",
        "playgroundai.com",
        "prompthero.com",
        "midjourney.com",
        "huggingface.co",
        "replicate.com",
    )
    social = (
        "x.com",
        "twitter.com",
        "facebook.com",
        "instagram.com",
        "tiktok.com",
        "reddit.com",
        "pinterest.com",
        "tumblr.com",
    )
    stock = (
        "shutterstock.com",
        "gettyimages.com",
        "istockphoto.com",
        "dreamstime.com",
        "alamy.com",
        "depositphotos.com",
        "123rf.com",
        "adobe.com",
    )

    if any(domain == item or domain.endswith(f".{item}") for item in ai_hosts):
        return "ai-hosting"
    if any(domain == item or domain.endswith(f".{item}") for item in social):
        return "social"
    if any(domain == item or domain.endswith(f".{item}") for item in stock):
        return "stock-media"
    if "news" in domain:
        return "news"
    return "other"


def _extract_year(crawl_date: str) -> int | None:
    if not crawl_date:
        return None

    prefix = crawl_date[:4]
    if len(prefix) == 4 and prefix.isdigit():
        return int(prefix)

    try:
        return datetime.fromisoformat(crawl_date.replace("Z", "+00:00")).year
    except ValueError:
        return None


def build_tineye_intelligence(
    matches: list[TinEyeMatch],
    *,
    top_n_domains: int = 5,
) -> dict[str, object]:
    domain_counter: Counter[str] = Counter()
    category_counter: Counter[str] = Counter()
    timeline_counter = {
        "pre-2020": 0,
        "2020-2022": 0,
        "2023+": 0,
    }

    for match in matches:
        domain = _extract_domain(match)
        if domain:
            domain_counter[domain] += 1
            category_counter[_classify_domain(domain)] += 1

        year = _extract_year(match.get("crawl_date", ""))
        if year is None:
            continue
        if year < 2020:
            timeline_counter["pre-2020"] += 1
        elif year <= 2022:
            timeline_counter["2020-2022"] += 1
        else:
            timeline_counter["2023+"] += 1

    top_domains = [
        {"domain": domain, "count": count}
        for domain, count in sorted(
            domain_counter.items(), key=lambda item: (-item[1], item[0])
        )[:top_n_domains]
    ]
    category_mix = [
        {"category": category, "count": count}
        for category, count in sorted(
            category_counter.items(), key=lambda item: (-item[1], item[0])
        )
    ]
    timeline_bins = [
        {"label": label, "count": count}
        for label, count in timeline_counter.items()
    ]

    return {
        "top_domains": top_domains,
        "category_mix": category_mix,
        "timeline_bins": timeline_bins,
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
            "intelligence": {
                "top_domains": [],
                "category_mix": [],
                "timeline_bins": [
                    {"label": "pre-2020", "count": 0},
                    {"label": "2020-2022", "count": 0},
                    {"label": "2023+", "count": 0},
                ],
            },
        }

    filtered = filter_matches_by_similarity(api_result["matches"])
    earliest = extract_earliest_date(filtered)
    buckets = bucket_matches(filtered, matchers)
    intelligence = build_tineye_intelligence(filtered)

    return {
        "success": True,
        "error": None,
        "total_matches": api_result["total_matches"],
        "filtered_match_count": len(filtered),
        "earliest_date": earliest,
        "on_shame_list": len(buckets["shame_list"]) > 0,
        "buckets": buckets,
        "intelligence": intelligence,
    }


def build_summary(
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


def get_tineye_status(context: AnalysisContext) -> dict[str, object]:
    return {
        "status": "MANUAL",
        "summary": "Search for this image across the web.",
        "data": {
            "allow_manual_refresh": True,
            "header_action": {
                "type": "htmx",
                "label": "Check TinEye",
            },
        },
    }



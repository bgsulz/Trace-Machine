from __future__ import annotations
import fnmatch
import logging
import re
import time
from dataclasses import dataclass
from typing import Protocol

import requests

logger = logging.getLogger(__name__)

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

import time

import pytest

from veracity.analyzers import tineye
from veracity.analyzers.tineye import (
    GlobMatcher,
    RegexMatcher,
    _parse_shame_list,
    _parse_regex_line,
    get_shame_list_matchers,
    url_matches_shame_list,
    clear_shame_list_cache,
    SHAME_LIST_TTL_SECONDS,
)


class TestParseShameList:
    def test_ignores_comments(self):
        raw = """
# This is a comment
# Another comment
*://*.example.com/*
        """
        matchers = _parse_shame_list(raw)
        assert len(matchers) == 1
        assert isinstance(matchers[0], GlobMatcher)

    def test_ignores_blank_lines(self):
        raw = """

*://*.example.com/*

*://*.other.com/*

        """
        matchers = _parse_shame_list(raw)
        assert len(matchers) == 2

    def test_handles_glob_patterns(self):
        raw = """
*://*.civitai.com/*
*://*.huggingface.co/*
*://*.reddit.com/r/StableDiffusion/*
        """
        matchers = _parse_shame_list(raw)
        assert len(matchers) == 3
        assert all(isinstance(m, GlobMatcher) for m in matchers)

    def test_handles_regex_patterns(self):
        raw = """
/amazon.+/.+/dp/B0C47SW6NT/
/stock.adobe.com/.*(ai-generated)/
        """
        matchers = _parse_shame_list(raw)
        assert len(matchers) == 2
        assert all(isinstance(m, RegexMatcher) for m in matchers)

    def test_handles_regex_with_case_insensitive_flag(self):
        raw = "/deviantart.com/.*ai-art/i"
        matchers = _parse_shame_list(raw)
        assert len(matchers) == 1
        assert isinstance(matchers[0], RegexMatcher)
        assert matchers[0].pattern.flags & 2  # re.IGNORECASE

    def test_skips_invalid_regex(self):
        raw = """
/valid.com/.*test/
/invalid[regex/
/another-valid.com/
        """
        matchers = _parse_shame_list(raw)
        assert len(matchers) == 2


class TestParseRegexLine:
    def test_simple_regex(self):
        regex_str, flags = _parse_regex_line("/amazon.+/dp/test/")
        assert regex_str == "amazon.+/dp/test"
        assert flags == 0

    def test_regex_with_case_insensitive_flag(self):
        regex_str, flags = _parse_regex_line("/pattern/i")
        assert regex_str == "pattern"
        assert flags == 2  # re.IGNORECASE

    def test_regex_without_trailing_slash(self):
        regex_str, flags = _parse_regex_line("/pattern")
        assert regex_str == "pattern"
        assert flags == 0

    def test_non_regex_returns_empty(self):
        regex_str, flags = _parse_regex_line("*://*.example.com/*")
        assert regex_str == ""
        assert flags == 0


class TestGlobMatcher:
    def test_matches_wildcard_subdomain(self):
        matcher = GlobMatcher(pattern="*://*.civitai.com/*")
        assert matcher.matches("https://images.civitai.com/gallery/123")
        assert matcher.matches("http://www.civitai.com/models")
        assert not matcher.matches("https://civitai.org/page")

    def test_matches_specific_path(self):
        matcher = GlobMatcher(pattern="*://*.reddit.com/r/StableDiffusion/*")
        assert matcher.matches("https://www.reddit.com/r/StableDiffusion/comments/abc")
        assert not matcher.matches("https://www.reddit.com/r/programming/comments/abc")

    def test_matches_any_protocol(self):
        matcher = GlobMatcher(pattern="*://*.example.com/*")
        assert matcher.matches("https://www.example.com/page")
        assert matcher.matches("http://sub.example.com/page")


class TestRegexMatcher:
    def test_matches_amazon_product(self):
        pattern = "/amazon.+/.+/dp/B0C47SW6NT/"
        _, flags = _parse_regex_line(pattern)
        matcher = RegexMatcher(pattern=__import__("re").compile("amazon.+/.+/dp/B0C47SW6NT", flags))
        assert matcher.matches("https://amazon.com/product/dp/B0C47SW6NT/ref=123")
        assert matcher.matches("https://amazon.co.uk/item/dp/B0C47SW6NT/")
        assert not matcher.matches("https://amazon.com/product/dp/B0COTHER/ref=123")

    def test_case_insensitive_match(self):
        import re
        matcher = RegexMatcher(pattern=re.compile("ai-art", re.IGNORECASE))
        assert matcher.matches("https://example.com/AI-ART/gallery")
        assert matcher.matches("https://example.com/ai-art/gallery")
        assert matcher.matches("https://example.com/Ai-Art/gallery")


class TestUrlMatchesShameList:
    def test_glob_match(self):
        matchers = [
            GlobMatcher(pattern="*://*.civitai.com/*"),
            GlobMatcher(pattern="*://*.huggingface.co/*"),
        ]
        assert url_matches_shame_list("https://www.civitai.com/models/123", matchers)
        assert url_matches_shame_list("https://images.civitai.com/gallery", matchers)
        assert url_matches_shame_list("https://www.huggingface.co/spaces/test", matchers)
        assert not url_matches_shame_list("https://github.com/repo", matchers)

    def test_regex_match(self):
        import re
        matchers = [
            RegexMatcher(pattern=re.compile(r"amazon.+/dp/B0C47SW6NT")),
        ]
        assert url_matches_shame_list("https://amazon.com/product/dp/B0C47SW6NT/", matchers)
        assert not url_matches_shame_list("https://amazon.com/product/dp/B0OTHER/", matchers)

    def test_no_match_returns_false(self):
        matchers = [
            GlobMatcher(pattern="*://*.badsite.com/*"),
        ]
        assert not url_matches_shame_list("https://goodsite.com/page", matchers)

    def test_empty_matchers_returns_false(self):
        assert not url_matches_shame_list("https://any-url.com/page", [])


class TestShameListCache:
    def test_cache_expires_after_ttl(self, monkeypatch):
        clear_shame_list_cache()

        call_count = 0
        def mock_fetch():
            nonlocal call_count
            call_count += 1
            return "*://*.example.com/*"

        monkeypatch.setattr(tineye, "_fetch_shame_list_raw", mock_fetch)

        matchers1 = get_shame_list_matchers()
        assert call_count == 1
        assert len(matchers1) == 1

        matchers2 = get_shame_list_matchers()
        assert call_count == 1

        monkeypatch.setattr(tineye, "_cached_at", time.time() - SHAME_LIST_TTL_SECONDS - 1)

        matchers3 = get_shame_list_matchers()
        assert call_count == 2

        clear_shame_list_cache()

    def test_force_refresh_bypasses_cache(self, monkeypatch):
        clear_shame_list_cache()

        call_count = 0
        def mock_fetch():
            nonlocal call_count
            call_count += 1
            return "*://*.test.com/*"

        monkeypatch.setattr(tineye, "_fetch_shame_list_raw", mock_fetch)

        get_shame_list_matchers()
        assert call_count == 1

        get_shame_list_matchers(force_refresh=True)
        assert call_count == 2

        clear_shame_list_cache()

    def test_returns_stale_cache_on_fetch_failure(self, monkeypatch):
        clear_shame_list_cache()

        monkeypatch.setattr(tineye, "_fetch_shame_list_raw", lambda: "*://*.cached.com/*")
        matchers1 = get_shame_list_matchers()
        assert len(matchers1) == 1

        monkeypatch.setattr(tineye, "_cached_at", time.time() - SHAME_LIST_TTL_SECONDS - 1)
        monkeypatch.setattr(tineye, "_fetch_shame_list_raw", lambda: "")

        matchers2 = get_shame_list_matchers()
        assert len(matchers2) == 1
        assert matchers2[0].pattern == "*://*.cached.com/*"

        clear_shame_list_cache()

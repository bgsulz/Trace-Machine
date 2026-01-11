import time

import pytest
import requests

from veracity.analyzers import tineye
from veracity.analyzers.tineye import (
    GlobMatcher,
    RegexMatcher,
    _parse_shame_list,
    _parse_regex_line,
    get_shame_list_matchers,
    url_matches_shame_list,
    clear_shame_list_cache,
    call_tineye_api,
    filter_matches_by_similarity,
    extract_earliest_date,
    bucket_matches,
    process_tineye_response,
    SHAME_LIST_TTL_SECONDS,
    SIMILARITY_THRESHOLD,
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


class TestCallTinEyeAPI:
    def test_missing_api_key_returns_error(self, monkeypatch):
        monkeypatch.delenv("TINEYE_KEY", raising=False)
        result = call_tineye_api(image_bytes=b"fake image data")
        assert result["success"] is False
        assert "missing TINEYE_KEY" in result["error"]

    def test_no_image_provided_returns_error(self, monkeypatch):
        monkeypatch.setenv("TINEYE_KEY", "test-key")
        result = call_tineye_api()
        assert result["success"] is False
        assert "No image provided" in result["error"]

    def test_api_request_failure_returns_error(self, monkeypatch):
        monkeypatch.setenv("TINEYE_KEY", "test-key")

        def mock_get(*args, **kwargs):
            raise requests.RequestException("Connection failed")

        monkeypatch.setattr(tineye.requests, "get", mock_get)

        result = call_tineye_api(image_url="https://example.com/image.jpg")
        assert result["success"] is False
        assert "API request failed" in result["error"]

    def test_api_error_code_returns_error(self, monkeypatch):
        monkeypatch.setenv("TINEYE_KEY", "test-key")

        class MockResponse:
            status_code = 200
            def raise_for_status(self):
                pass
            def json(self):
                return {"code": 400, "messages": ["Invalid image format"]}

        monkeypatch.setattr(tineye.requests, "get", lambda *a, **k: MockResponse())

        result = call_tineye_api(image_url="https://example.com/image.jpg")
        assert result["success"] is False
        assert "Invalid image format" in result["error"]

    def test_successful_response_with_url(self, monkeypatch):
        monkeypatch.setenv("TINEYE_KEY", "test-key")

        class MockResponse:
            status_code = 200
            def raise_for_status(self):
                pass
            def json(self):
                return {
                    "code": 200,
                    "results": {
                        "total_results": 2,
                        "matches": [
                            {
                                "domain": "example.com",
                                "score": 85,
                                "backlinks": [
                                    {"url": "https://example.com/page", "crawl_date": "2023-06-15"}
                                ],
                            },
                            {
                                "domain": "other.com",
                                "score": 60,
                                "backlinks": [
                                    {"url": "https://other.com/img", "crawl_date": "2024-01-10"}
                                ],
                            },
                        ],
                    },
                }

        monkeypatch.setattr(tineye.requests, "get", lambda *a, **k: MockResponse())

        result = call_tineye_api(image_url="https://example.com/image.jpg")
        assert result["success"] is True
        assert result["total_matches"] == 2
        assert len(result["matches"]) == 2
        assert result["matches"][0]["domain"] == "example.com"
        assert result["matches"][0]["similarity"] == 0.85
        assert result["matches"][0]["crawl_date"] == "2023-06-15T00:00:00"

    def test_successful_response_with_bytes(self, monkeypatch):
        monkeypatch.setenv("TINEYE_KEY", "test-key")

        class MockResponse:
            status_code = 200
            def raise_for_status(self):
                pass
            def json(self):
                return {
                    "code": 200,
                    "results": {"total_results": 0, "matches": []},
                }

        monkeypatch.setattr(tineye.requests, "post", lambda *a, **k: MockResponse())

        result = call_tineye_api(image_bytes=b"fake image data")
        assert result["success"] is True
        assert result["total_matches"] == 0
        assert result["matches"] == []


class TestFilterMatchesBySimilarity:
    def test_filters_below_threshold(self):
        matches = [
            {"url": "a", "domain": "a.com", "crawl_date": "2023-01-01T00:00:00", "similarity": 0.5},
            {"url": "b", "domain": "b.com", "crawl_date": "2023-02-01T00:00:00", "similarity": 0.3},
            {"url": "c", "domain": "c.com", "crawl_date": "2023-03-01T00:00:00", "similarity": 0.45},
        ]
        filtered = filter_matches_by_similarity(matches)
        assert len(filtered) == 2
        assert all(m["similarity"] >= 0.4 for m in filtered)

    def test_custom_threshold(self):
        matches = [
            {"url": "a", "domain": "a.com", "crawl_date": "2023-01-01T00:00:00", "similarity": 0.8},
            {"url": "b", "domain": "b.com", "crawl_date": "2023-02-01T00:00:00", "similarity": 0.6},
        ]
        filtered = filter_matches_by_similarity(matches, threshold=0.7)
        assert len(filtered) == 1
        assert filtered[0]["url"] == "a"

    def test_empty_list(self):
        assert filter_matches_by_similarity([]) == []


class TestExtractEarliestDate:
    def test_returns_earliest(self):
        matches = [
            {"url": "a", "domain": "a.com", "crawl_date": "2023-06-15T00:00:00", "similarity": 0.5},
            {"url": "b", "domain": "b.com", "crawl_date": "2022-01-01T00:00:00", "similarity": 0.5},
            {"url": "c", "domain": "c.com", "crawl_date": "2024-03-20T00:00:00", "similarity": 0.5},
        ]
        earliest = extract_earliest_date(matches)
        assert earliest == "2022-01-01T00:00:00"

    def test_empty_list_returns_none(self):
        assert extract_earliest_date([]) is None

    def test_no_dates_returns_none(self):
        matches = [
            {"url": "a", "domain": "a.com", "crawl_date": "", "similarity": 0.5},
        ]
        assert extract_earliest_date(matches) is None


class TestBucketMatches:
    def test_buckets_oldest_and_newest(self):
        matches = [
            {"url": f"url{i}", "domain": f"d{i}.com", "crawl_date": f"2020-{i+1:02d}-01T00:00:00", "similarity": 0.5}
            for i in range(10)
        ]
        buckets = bucket_matches(matches, matchers=[])
        
        assert len(buckets["oldest"]) == 5
        assert buckets["oldest"][0]["crawl_date"] == "2020-01-01T00:00:00"
        
        assert len(buckets["newest"]) == 5
        assert buckets["newest"][0]["crawl_date"] == "2020-10-01T00:00:00"

    def test_fewer_than_5_matches(self):
        matches = [
            {"url": "a", "domain": "a.com", "crawl_date": "2023-01-01T00:00:00", "similarity": 0.5},
            {"url": "b", "domain": "b.com", "crawl_date": "2023-02-01T00:00:00", "similarity": 0.5},
        ]
        buckets = bucket_matches(matches, matchers=[])
        
        assert len(buckets["oldest"]) == 2
        assert len(buckets["newest"]) == 0

    def test_shame_list_detection(self):
        matches = [
            {"url": "https://www.civitai.com/image/123", "domain": "civitai.com", "crawl_date": "2023-01-01T00:00:00", "similarity": 0.5},
            {"url": "https://www.github.com/repo", "domain": "github.com", "crawl_date": "2023-02-01T00:00:00", "similarity": 0.5},
        ]
        matchers = [GlobMatcher(pattern="*://*.civitai.com/*")]
        buckets = bucket_matches(matches, matchers=matchers)
        
        assert len(buckets["shame_list"]) == 1
        assert buckets["shame_list"][0]["domain"] == "civitai.com"


class TestProcessTinEyeResponse:
    def test_failed_api_result(self):
        api_result = {
            "success": False,
            "error": "API failed",
            "total_matches": 0,
            "matches": [],
        }
        processed = process_tineye_response(api_result)
        assert processed["success"] is False
        assert processed["error"] == "API failed"

    def test_successful_processing(self):
        api_result = {
            "success": True,
            "error": None,
            "total_matches": 50,
            "matches": [
                {"url": "https://www.civitai.com/img", "domain": "civitai.com", "crawl_date": "2022-01-01T00:00:00", "similarity": 0.8},
                {"url": "https://example.com/img", "domain": "example.com", "crawl_date": "2023-06-15T00:00:00", "similarity": 0.5},
                {"url": "https://other.com/img", "domain": "other.com", "crawl_date": "2024-01-01T00:00:00", "similarity": 0.3},
            ],
        }
        matchers = [GlobMatcher(pattern="*://*.civitai.com/*")]
        processed = process_tineye_response(api_result, matchers=matchers)
        
        assert processed["success"] is True
        assert processed["total_matches"] == 50
        assert processed["earliest_date"] == "2022-01-01T00:00:00"
        assert processed["on_shame_list"] is True
        assert len(processed["buckets"]["shame_list"]) == 1

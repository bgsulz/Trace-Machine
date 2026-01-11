import json
import time
from datetime import datetime, timedelta, UTC
from types import SimpleNamespace

import pytest
import requests

from veracity import db, limiter
from veracity.analyzers import tineye
from veracity.analyzers.context import AnalysisContext
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
    get_tineye_status,
    execute_tineye_search,
    _is_result_stale,
    _build_summary,
    SHAME_LIST_TTL_SECONDS,
    SIMILARITY_THRESHOLD,
    STALE_THRESHOLD_DAYS,
)
from veracity.models import ImageRegistry, TinEyeResult


class TestLimiterInitialized:
    def test_limiter_initialized(self, app):
        with app.app_context():
            assert limiter is not None
            assert hasattr(limiter, "limit")


class TestAnalyzerRegistration:
    def test_tineye_analyzer_in_manager(self):
        from veracity.analyzers.manager import ANALYZERS, get_analyzer_spec

        slugs = [spec.slug for spec in ANALYZERS]
        assert "tineye" in slugs

        spec = get_analyzer_spec("tineye")
        assert spec is not None
        assert spec.name == "TinEye"
        assert spec.template == "partials/analyzers/tineye.html"

    def test_context_includes_tineye_neighbor_data(self, app):
        from veracity.registry import TinEyeSnapshot, NeighborSnapshot

        snapshot = NeighborSnapshot(
            id=1,
            phash="abc123",
            whash="def456",
            created_at=None,
            consensus=None,
            sources=(),
            facts=(),
            tineye_result=TinEyeSnapshot(
                total_matches=10,
                earliest_date=None,
                on_shame_list=True,
            ),
        )
        assert snapshot.tineye_result is not None
        assert snapshot.tineye_result.total_matches == 10
        assert snapshot.tineye_result.on_shame_list is True


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


def _make_context(registry_id: int, neighbors=None):
    return AnalysisContext(
        image_bytes=b"test image",
        phash="0011ffaa0011ffaa",
        whash="0011ffaa0011ffbb",
        registry_id=registry_id,
        neighbors=neighbors or [],
        width=100,
        height=100,
    )


def _create_registry(app):
    with app.app_context():
        registry = ImageRegistry(phash="0011ffaa0011ffaa", whash="0011ffaa0011ffbb")
        db.session.add(registry)
        db.session.commit()
        return registry.id


class TestIsResultStale:
    def test_no_searched_at_is_stale(self, app):
        with app.app_context():
            result = TinEyeResult(
                image_id=1,
                total_matches=0,
                on_shame_list=False,
                matches_json="{}",
                searched_at=None,
            )
            assert _is_result_stale(result) is True

    def test_old_result_is_stale(self, app):
        with app.app_context():
            old_date = datetime.now(UTC) - timedelta(days=STALE_THRESHOLD_DAYS + 1)
            result = TinEyeResult(
                image_id=1,
                total_matches=0,
                on_shame_list=False,
                matches_json="{}",
                searched_at=old_date,
            )
            assert _is_result_stale(result) is True

    def test_recent_result_is_not_stale(self, app):
        with app.app_context():
            recent_date = datetime.now(UTC) - timedelta(days=30)
            result = TinEyeResult(
                image_id=1,
                total_matches=0,
                on_shame_list=False,
                matches_json="{}",
                searched_at=recent_date,
            )
            assert _is_result_stale(result) is False


class TestBuildSummary:
    def test_no_matches(self):
        summary = _build_summary(0, None, False)
        assert summary == "No matches found."

    def test_with_matches_and_date(self):
        summary = _build_summary(10, "2022-06-15T00:00:00", False)
        assert "10 matches found." in summary
        assert "Jun 2022" in summary
        assert "Not on known AI sites." in summary

    def test_with_shame_list(self):
        summary = _build_summary(5, "2023-01-01T00:00:00", True)
        assert "⚠️ Found on AI image sites." in summary


class TestGetTinEyeStatus:
    def test_returns_waiting_when_no_result(self, app):
        registry_id = _create_registry(app)
        context = _make_context(registry_id)

        with app.app_context():
            result = get_tineye_status(context)

        assert result["status"] == "WAITING"
        assert "Manual check required" in result["summary"]

    def test_returns_found_with_cached_result(self, app):
        registry_id = _create_registry(app)

        with app.app_context():
            tineye_result = TinEyeResult(
                image_id=registry_id,
                total_matches=25,
                earliest_date=datetime(2022, 6, 15, tzinfo=UTC),
                on_shame_list=False,
                matches_json=json.dumps({"oldest": [], "newest": [], "shame_list": []}),
                searched_at=datetime.now(UTC),
            )
            db.session.add(tineye_result)
            db.session.commit()

        context = _make_context(registry_id)

        with app.app_context():
            result = get_tineye_status(context)

        assert result["status"] == "FOUND"
        assert "25 matches found" in result["summary"]

    def test_returns_not_found_when_zero_matches(self, app):
        registry_id = _create_registry(app)

        with app.app_context():
            tineye_result = TinEyeResult(
                image_id=registry_id,
                total_matches=0,
                earliest_date=None,
                on_shame_list=False,
                matches_json=json.dumps({"oldest": [], "newest": [], "shame_list": []}),
                searched_at=datetime.now(UTC),
            )
            db.session.add(tineye_result)
            db.session.commit()

        context = _make_context(registry_id)

        with app.app_context():
            result = get_tineye_status(context)

        assert result["status"] == "NOT FOUND"

    def test_returns_stale_when_old(self, app):
        registry_id = _create_registry(app)

        with app.app_context():
            old_date = datetime.now(UTC) - timedelta(days=STALE_THRESHOLD_DAYS + 1)
            tineye_result = TinEyeResult(
                image_id=registry_id,
                total_matches=10,
                earliest_date=datetime(2022, 1, 1, tzinfo=UTC),
                on_shame_list=False,
                matches_json=json.dumps({"oldest": [], "newest": [], "shame_list": []}),
                searched_at=old_date,
            )
            db.session.add(tineye_result)
            db.session.commit()

        context = _make_context(registry_id)

        with app.app_context():
            result = get_tineye_status(context)

        assert result["status"] == "STALE"

    def test_includes_neighbor_matches(self, app):
        registry_id = _create_registry(app)

        neighbor_tineye = SimpleNamespace(
            total_matches=5,
            earliest_date=datetime(2023, 1, 1, tzinfo=UTC),
            on_shame_list=True,
            matches_json=json.dumps({"oldest": [], "newest": [], "shame_list": []}),
        )
        neighbor = SimpleNamespace(
            phash="1122334455667788",
            whash="1122334455667799",
            tineye_result=neighbor_tineye,
            sources=[],
        )
        context = _make_context(registry_id, neighbors=[neighbor])

        with app.app_context():
            result = get_tineye_status(context)

        assert result["status"] == "WAITING"
        assert len(result["data"]["matches"]) == 1
        assert result["data"]["matches"][0]["total_matches"] == 5


class TestExecuteTinEyeSearch:
    def test_creates_result(self, app, monkeypatch):
        registry_id = _create_registry(app)
        context = _make_context(registry_id)

        def mock_call_api(**kwargs):
            return {
                "success": True,
                "error": None,
                "total_matches": 15,
                "matches": [
                    {"url": "https://example.com/img", "domain": "example.com", "crawl_date": "2022-03-15T00:00:00", "similarity": 0.8},
                ],
            }

        monkeypatch.setattr(tineye, "call_tineye_api", mock_call_api)
        monkeypatch.setattr(tineye, "get_shame_list_matchers", lambda **k: [])

        with app.app_context():
            result = execute_tineye_search("analysis-123", context)
            saved = TinEyeResult.query.filter_by(image_id=registry_id).first()

        assert result["status"] in ("FOUND", "NOT FOUND")
        assert saved is not None
        assert saved.total_matches == 15

    def test_replaces_stale_result(self, app, monkeypatch):
        registry_id = _create_registry(app)

        with app.app_context():
            old_date = datetime.now(UTC) - timedelta(days=STALE_THRESHOLD_DAYS + 1)
            old_result = TinEyeResult(
                image_id=registry_id,
                total_matches=5,
                earliest_date=None,
                on_shame_list=False,
                matches_json=json.dumps({"oldest": [], "newest": [], "shame_list": []}),
                searched_at=old_date,
            )
            db.session.add(old_result)
            db.session.commit()

        context = _make_context(registry_id)

        def mock_call_api(**kwargs):
            return {
                "success": True,
                "error": None,
                "total_matches": 30,
                "matches": [],
            }

        monkeypatch.setattr(tineye, "call_tineye_api", mock_call_api)
        monkeypatch.setattr(tineye, "get_shame_list_matchers", lambda **k: [])

        with app.app_context():
            result = execute_tineye_search("analysis-123", context)
            updated = TinEyeResult.query.filter_by(image_id=registry_id).first()

        assert updated.total_matches == 30

    def test_skips_if_fresh_exists(self, app, monkeypatch):
        registry_id = _create_registry(app)

        with app.app_context():
            fresh_result = TinEyeResult(
                image_id=registry_id,
                total_matches=20,
                earliest_date=None,
                on_shame_list=False,
                matches_json=json.dumps({"oldest": [], "newest": [], "shame_list": []}),
                searched_at=datetime.now(UTC),
            )
            db.session.add(fresh_result)
            db.session.commit()

        context = _make_context(registry_id)
        api_called = []

        def mock_call_api(**kwargs):
            api_called.append(True)
            return {"success": True, "error": None, "total_matches": 999, "matches": []}

        monkeypatch.setattr(tineye, "call_tineye_api", mock_call_api)

        with app.app_context():
            result = execute_tineye_search("analysis-123", context)

        assert len(api_called) == 0
        assert result["data"]["total_matches"] == 20

    def test_returns_error_on_api_failure(self, app, monkeypatch):
        registry_id = _create_registry(app)
        context = _make_context(registry_id)

        def mock_call_api(**kwargs):
            return {
                "success": False,
                "error": "API request failed",
                "total_matches": 0,
                "matches": [],
            }

        monkeypatch.setattr(tineye, "call_tineye_api", mock_call_api)

        with app.app_context():
            result = execute_tineye_search("analysis-123", context)

        assert result["status"] == "ERROR"
        assert "API request failed" in result["summary"]


class TestRunTinEyeRoute:
    def test_run_tineye_route_expired_analysis(self, app):
        client = app.test_client()
        response = client.post("/analysis/nonexistent-id/tineye/run")
        assert response.status_code == 410

    def test_run_tineye_route_success(self, app, monkeypatch):
        from veracity import routes
        from io import BytesIO
        from PIL import Image

        img = Image.new("RGB", (100, 100), color=(255, 0, 0))
        buf = BytesIO()
        img.save(buf, format="PNG")
        image_bytes = buf.getvalue()

        monkeypatch.setattr(
            routes,
            "load_analysis_payload",
            lambda aid: (image_bytes, {"mime_type": "image/png", "source": "file"}),
        )

        def mock_execute(analysis_id, context):
            return {
                "status": "NOT FOUND",
                "summary": "No matches found.",
                "data": {"total_matches": 0, "matches": []},
            }

        monkeypatch.setattr(routes, "execute_tineye_search", mock_execute)

        client = app.test_client()
        response = client.post("/analysis/test-id/tineye/run")
        assert response.status_code == 200

    def test_run_tineye_route_rate_limited(self, app, monkeypatch):
        from veracity import routes
        from io import BytesIO
        from PIL import Image

        img = Image.new("RGB", (100, 100), color=(255, 0, 0))
        buf = BytesIO()
        img.save(buf, format="PNG")
        image_bytes = buf.getvalue()

        monkeypatch.setattr(
            routes,
            "load_analysis_payload",
            lambda aid: (image_bytes, {"mime_type": "image/png", "source": "file"}),
        )

        def mock_execute(analysis_id, context):
            return {
                "status": "NOT FOUND",
                "summary": "No matches found.",
                "data": {"total_matches": 0, "matches": []},
            }

        monkeypatch.setattr(routes, "execute_tineye_search", mock_execute)

        client = app.test_client()

        for i in range(5):
            response = client.post("/analysis/test-id/tineye/run")
            assert response.status_code == 200, f"Request {i+1} failed unexpectedly"

        response = client.post("/analysis/test-id/tineye/run")
        assert response.status_code == 429

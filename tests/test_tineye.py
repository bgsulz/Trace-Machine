from types import SimpleNamespace

from veracity import db
from veracity.analysis_cache import analysis_row_path, store_analysis_payload
from veracity.analyzers.context import AnalysisContext
from veracity.analyzers.tineye import (
    build_tineye_intelligence,
    get_tineye_status,
    process_tineye_response,
)
from veracity.models import ImageRegistry


class TestAnalyzerRegistration:
    def test_tineye_analyzer_in_manager(self):
        from veracity.analyzers.manager import ANALYZERS, get_analyzer_spec

        slugs = [spec.slug for spec in ANALYZERS]
        assert "tineye" in slugs

        spec = get_analyzer_spec("tineye")
        assert spec is not None
        assert spec.name == "TinEye Reverse Search"
        assert spec.template == "partials/analyzers/tineye.html"


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


class TestGetTinEyeStatus:
    def test_returns_manual_when_no_result(self, app):
        registry_id = _create_registry(app)
        context = _make_context(registry_id)

        with app.app_context():
            result = get_tineye_status(context)

        assert result["status"] == "MANUAL"
        assert "Search for this image" in result["summary"]
        assert result["data"]["allow_manual_refresh"] is True

    def test_ignores_neighbors(self, app):
        """TinEye status ignores neighbors - it always returns MANUAL
        since the actual lookup requires user action."""
        registry_id = _create_registry(app)

        neighbor = SimpleNamespace(
            phash="1122334455667788",
            whash="1122334455667799",
            sources=[],
        )
        context = _make_context(registry_id, neighbors=[neighbor])

        with app.app_context():
            result = get_tineye_status(context)

        assert result["status"] == "MANUAL"
        assert result["data"]["allow_manual_refresh"] is True


class TestTinEyeEnrichment:
    def test_build_tineye_intelligence_counts_domains_categories_and_timeline(self):
        matches = [
            {
                "url": "https://x.com/example/status/1",
                "domain": "x.com",
                "crawl_date": "2019-04-01T00:00:00",
                "similarity": 0.95,
            },
            {
                "url": "https://example-news.com/story",
                "domain": "example-news.com",
                "crawl_date": "2021-05-01T00:00:00",
                "similarity": 0.88,
            },
            {
                "url": "https://civitai.com/images/1",
                "domain": "civitai.com",
                "crawl_date": "2024-01-01T00:00:00",
                "similarity": 0.93,
            },
            {
                "url": "https://civitai.com/images/2",
                "domain": "civitai.com",
                "crawl_date": "2024-06-01T00:00:00",
                "similarity": 0.91,
            },
        ]

        result = build_tineye_intelligence(matches)

        assert result["top_domains"][0] == {"domain": "civitai.com", "count": 2}
        assert {"category": "ai-hosting", "count": 2} in result["category_mix"]
        assert {"category": "social", "count": 1} in result["category_mix"]
        assert {"category": "news", "count": 1} in result["category_mix"]
        assert result["timeline_bins"] == [
            {"label": "pre-2020", "count": 1},
            {"label": "2020-2022", "count": 1},
            {"label": "2023+", "count": 2},
        ]

    def test_process_response_includes_intelligence(self):
        api_result = {
            "success": True,
            "error": None,
            "total_matches": 2,
            "matches": [
                {
                    "url": "https://example.com/a",
                    "domain": "example.com",
                    "crawl_date": "2022-01-01T00:00:00",
                    "similarity": 0.80,
                },
                {
                    "url": "https://example.com/b",
                    "domain": "example.com",
                    "crawl_date": "2024-01-01T00:00:00",
                    "similarity": 0.92,
                },
            ],
        }

        processed = process_tineye_response(api_result, matchers=[])

        assert "intelligence" in processed
        assert processed["intelligence"]["top_domains"][0] == {
            "domain": "example.com",
            "count": 2,
        }


class TestRunTinEyeRoute:
    def test_run_tineye_route_expired_analysis(self, app):
        client = app.test_client()
        response = client.post("/analysis/nonexistent-id/tineye/run")
        assert response.status_code == 410

    def test_run_tineye_route_success(self, app, monkeypatch):
        from veracity.route_groups import analysis as analysis_routes
        from io import BytesIO
        from PIL import Image

        img = Image.new("RGB", (100, 100), color=(255, 0, 0))
        buf = BytesIO()
        img.save(buf, format="PNG")
        image_bytes = buf.getvalue()

        monkeypatch.setattr(
            analysis_routes,
            "load_analysis_payload",
            lambda aid: (image_bytes, {"mime_type": "image/png", "source": "file"}),
        )

        def mock_call_api(**kwargs):
            return {
                "success": True,
                "error": None,
                "total_matches": 0,
                "matches": [],
            }

        def mock_process_response(api_result, **kwargs):
            return {
                "success": True,
                "error": None,
                "total_matches": 0,
                "filtered_match_count": 0,
                "earliest_date": None,
                "on_shame_list": False,
                "buckets": {"oldest": [], "newest": [], "shame_list": []},
            }

        def mock_get_matchers(**kwargs):
            return []

        monkeypatch.setattr(analysis_routes, "call_tineye_api", mock_call_api)
        monkeypatch.setattr(analysis_routes, "process_tineye_response", mock_process_response)
        monkeypatch.setattr(analysis_routes, "get_shame_list_matchers", mock_get_matchers)

        client = app.test_client()
        response = client.post("/analysis/test-id/tineye/run")
        assert response.status_code == 200

    def test_run_tineye_route_rate_limited(self, app_ratelimited, monkeypatch):
        from veracity.route_groups import analysis as analysis_routes
        from io import BytesIO
        from PIL import Image

        app = app_ratelimited

        img = Image.new("RGB", (100, 100), color=(255, 0, 0))
        buf = BytesIO()
        img.save(buf, format="PNG")
        image_bytes = buf.getvalue()

        monkeypatch.setattr(
            analysis_routes,
            "load_analysis_payload",
            lambda aid: (image_bytes, {"mime_type": "image/png", "source": "file"}),
        )

        def mock_call_api(**kwargs):
            return {
                "success": True,
                "error": None,
                "total_matches": 0,
                "matches": [],
            }

        def mock_process_response(api_result, **kwargs):
            return {
                "success": True,
                "error": None,
                "total_matches": 0,
                "filtered_match_count": 0,
                "earliest_date": None,
                "on_shame_list": False,
                "buckets": {"oldest": [], "newest": [], "shame_list": []},
            }

        def mock_get_matchers(**kwargs):
            return []

        monkeypatch.setattr(analysis_routes, "call_tineye_api", mock_call_api)
        monkeypatch.setattr(analysis_routes, "process_tineye_response", mock_process_response)
        monkeypatch.setattr(analysis_routes, "get_shame_list_matchers", mock_get_matchers)

        client = app.test_client()
        headers = {"HX-Request": "true"}  # Simulate HTMX request

        for i in range(5):
            response = client.post("/analysis/test-id/tineye/run", headers=headers)
            assert response.status_code == 200, f"Request {i+1} failed unexpectedly"

        response = client.post("/analysis/test-id/tineye/run", headers=headers)
        assert response.status_code == 429

    def test_run_tineye_route_none_mode_has_no_cache_side_effect(self, app, monkeypatch):
        from veracity.route_groups import analysis as analysis_routes
        from io import BytesIO
        from PIL import Image

        app.config["TINEYE_PERSISTENCE_MODE"] = "none"

        img = Image.new("RGB", (100, 100), color=(255, 0, 0))
        buf = BytesIO()
        img.save(buf, format="PNG")
        image_bytes = buf.getvalue()

        monkeypatch.setattr(
            analysis_routes,
            "load_analysis_payload",
            lambda aid: (image_bytes, {"mime_type": "image/png", "source": "file"}),
        )
        monkeypatch.setattr(
            analysis_routes,
            "call_tineye_api",
            lambda **kwargs: {"success": True, "error": None, "total_matches": 0, "matches": []},
        )
        monkeypatch.setattr(
            analysis_routes,
            "process_tineye_response",
            lambda api_result, **kwargs: {
                "success": True,
                "error": None,
                "total_matches": 0,
                "filtered_match_count": 0,
                "earliest_date": None,
                "on_shame_list": False,
                "buckets": {"oldest": [], "newest": [], "shame_list": []},
                "intelligence": {
                    "top_domains": [],
                    "category_mix": [],
                    "timeline_bins": [],
                },
            },
        )
        monkeypatch.setattr(analysis_routes, "get_shame_list_matchers", lambda **kwargs: [])

        client = app.test_client()
        response = client.post("/analysis/test-id/tineye/run")
        assert response.status_code == 200

        with app.app_context():
            assert not analysis_row_path("test-id", "tineye").exists()


class TestTinEyePersistenceMode:
    def test_tineye_fragment_skips_row_cache_in_none_mode(self, app):
        from io import BytesIO
        from PIL import Image

        app.config["TINEYE_PERSISTENCE_MODE"] = "none"

        img = Image.new("RGB", (100, 100), color=(255, 0, 0))
        buf = BytesIO()
        img.save(buf, format="PNG")
        image_bytes = buf.getvalue()

        with app.app_context():
            analysis_id = store_analysis_payload(
                "cache-none-id",
                image_bytes,
                {"mime_type": "image/png", "source": "file"},
            )

        client = app.test_client()
        tineye_response = client.get(f"/analysis/{analysis_id}/analyzers/tineye")
        assert tineye_response.status_code == 200

        exif_response = client.get(f"/analysis/{analysis_id}/analyzers/exif")
        assert exif_response.status_code == 200

        with app.app_context():
            assert not analysis_row_path(analysis_id, "tineye").exists()
            assert analysis_row_path(analysis_id, "exif").exists()

    def test_tineye_fragment_caches_row_in_derived_mode(self, app):
        from io import BytesIO
        from PIL import Image

        app.config["TINEYE_PERSISTENCE_MODE"] = "derived"

        img = Image.new("RGB", (100, 100), color=(255, 0, 0))
        buf = BytesIO()
        img.save(buf, format="PNG")
        image_bytes = buf.getvalue()

        with app.app_context():
            analysis_id = store_analysis_payload(
                "cache-derived-id",
                image_bytes,
                {"mime_type": "image/png", "source": "file"},
            )

        client = app.test_client()
        tineye_response = client.get(f"/analysis/{analysis_id}/analyzers/tineye")
        assert tineye_response.status_code == 200

        with app.app_context():
            assert analysis_row_path(analysis_id, "tineye").exists()


class TestTinEyeTemplates:
    def test_tineye_template_renders_manual_state(self, app):
        from flask import render_template

        with app.test_request_context():
            row = {
                "status": "MANUAL",
                "summary": "Search for this image across the web.",
                "data": {},
                "context": {"analysis_id": "test-123", "link_target": "_blank"},
            }
            html = render_template("partials/analyzers/tineye.html", row=row)
            # Template shows instructions to click header button
            assert "Check TinEye" in html
            assert "search for this image" in html

    def test_tineye_template_renders_found_state(self, app):
        from flask import render_template

        with app.test_request_context():
            row = {
                "status": "FOUND",
                "summary": "10 matches found",
                "data": {"buckets": {"oldest": [], "newest": [], "shame_list": []}},
                "context": {"analysis_id": "test-123", "link_target": "_blank"},
            }
            html = render_template("partials/analyzers/tineye.html", row=row)
            assert "Check TinEye" not in html

    def test_tineye_template_renders_error_state(self, app):
        from flask import render_template

        with app.test_request_context():
            row = {
                "status": "ERROR",
                "summary": "API error",
                "data": {"buckets": {"oldest": [], "newest": [], "shame_list": []}},
                "context": {"analysis_id": "test-123", "link_target": "_blank"},
            }
            html = render_template("partials/analyzers/tineye.html", row=row)
            # ERROR state renders buckets (empty in this case), not MANUAL instructions
            assert "Check TinEye" not in html
            assert "analyzer-detail-block" in html


class TestShameListParsing:
    """Tests for shame list parsing and URL matching."""

    def test_glob_matcher_matches_subdomain(self):
        from veracity.analyzers.tineye import GlobMatcher

        matcher = GlobMatcher("*://*.civitai.com/*")
        assert matcher.matches("https://www.civitai.com/images/123")
        assert matcher.matches("https://cdn.civitai.com/images/123")
        assert matcher.matches("http://images.civitai.com/foo")

    def test_glob_matcher_matches_apex_domain(self):
        from veracity.analyzers.tineye import GlobMatcher

        matcher = GlobMatcher("*://*.civitai.com/*")
        # Apex domains should match due to the fix
        assert matcher.matches("https://civitai.com/images/123")
        assert matcher.matches("http://civitai.com/")

    def test_glob_matcher_rejects_non_matching_domains(self):
        from veracity.analyzers.tineye import GlobMatcher

        matcher = GlobMatcher("*://*.civitai.com/*")
        assert not matcher.matches("https://example.com/civitai.com/")
        assert not matcher.matches("https://notcivitai.com/images")
        assert not matcher.matches("https://civitai.org/images")

    def test_regex_matcher_basic(self):
        from veracity.analyzers.tineye import RegexMatcher
        import re

        matcher = RegexMatcher(re.compile(r"civitai\.com"))
        assert matcher.matches("https://civitai.com/images/123")
        assert matcher.matches("https://www.civitai.com/foo")
        assert not matcher.matches("https://example.com/")

    def test_regex_matcher_case_insensitive(self):
        from veracity.analyzers.tineye import RegexMatcher
        import re

        matcher = RegexMatcher(re.compile(r"civitai\.com", re.IGNORECASE))
        assert matcher.matches("https://CIVITAI.COM/images")
        assert matcher.matches("https://CiViTaI.cOm/foo")

    def test_parse_regex_line_basic(self):
        from veracity.analyzers.tineye import _parse_regex_line

        regex_str, flags = _parse_regex_line("/civitai\\.com/")
        assert regex_str == "civitai\\.com"
        assert flags == 0

    def test_parse_regex_line_case_insensitive(self):
        from veracity.analyzers.tineye import _parse_regex_line
        import re

        regex_str, flags = _parse_regex_line("/civitai\\.com/i")
        assert regex_str == "civitai\\.com"
        assert flags == re.IGNORECASE

    def test_parse_regex_line_non_regex(self):
        from veracity.analyzers.tineye import _parse_regex_line

        regex_str, flags = _parse_regex_line("*://example.com/*")
        assert regex_str == ""
        assert flags == 0

    def test_parse_shame_list_mixed_patterns(self):
        from veracity.analyzers.tineye import _parse_shame_list, GlobMatcher, RegexMatcher

        raw_text = """# Comment line
*://*.civitai.com/*
/artstation\\.com\\/artwork/i

# Another comment
*://*.huggingface.co/*
"""
        matchers = _parse_shame_list(raw_text)

        assert len(matchers) == 3
        assert isinstance(matchers[0], GlobMatcher)
        assert isinstance(matchers[1], RegexMatcher)
        assert isinstance(matchers[2], GlobMatcher)

    def test_parse_shame_list_skips_empty_and_comments(self):
        from veracity.analyzers.tineye import _parse_shame_list

        raw_text = """
# This is a comment
   # Indented comment

*://*.example.com/*

"""
        matchers = _parse_shame_list(raw_text)
        assert len(matchers) == 1

    def test_parse_shame_list_handles_invalid_regex(self):
        from veracity.analyzers.tineye import _parse_shame_list

        raw_text = """*://*.valid.com/*
/[invalid(regex/
*://*.another.com/*
"""
        # Should not raise, should skip invalid regex
        matchers = _parse_shame_list(raw_text)
        assert len(matchers) == 2  # Only the two valid glob patterns

    def test_url_matches_shame_list_with_custom_matchers(self):
        from veracity.analyzers.tineye import url_matches_shame_list, GlobMatcher

        matchers = [
            GlobMatcher("*://*.civitai.com/*"),
            GlobMatcher("*://*.artbreeder.com/*"),
        ]

        assert url_matches_shame_list("https://civitai.com/images/123", matchers)
        assert url_matches_shame_list("https://www.artbreeder.com/foo", matchers)
        assert not url_matches_shame_list("https://example.com/", matchers)

    def test_url_matches_shame_list_empty_matchers(self):
        from veracity.analyzers.tineye import url_matches_shame_list

        assert not url_matches_shame_list("https://anything.com/", [])

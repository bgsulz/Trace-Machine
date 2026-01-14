import json
from datetime import datetime, timedelta, UTC
from types import SimpleNamespace

from veracity import db
from veracity.analyzers import tineye
from veracity.analyzers.context import AnalysisContext
from veracity.analyzers.tineye import (
    get_tineye_status,
    execute_tineye_search,
    STALE_THRESHOLD_DAYS,
)
from veracity.models import ImageRegistry, TinEyeResult


class TestAnalyzerRegistration:
    def test_tineye_analyzer_in_manager(self):
        from veracity.analyzers.manager import ANALYZERS, get_analyzer_spec

        slugs = [spec.slug for spec in ANALYZERS]
        assert "tineye" in slugs

        spec = get_analyzer_spec("tineye")
        assert spec is not None
        assert spec.name == "TinEye"
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
                matches_json=json.dumps({"oldest": [{"url": "test"}], "newest": [], "shame_list": []}),
                searched_at=datetime.now(UTC),
            )
            db.session.add(tineye_result)
            db.session.commit()

        context = _make_context(registry_id)

        with app.app_context():
            result = get_tineye_status(context)

        assert result["status"] == "FOUND"
        assert "1 matches found" in result["summary"]

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

    def test_zero_matches_allows_manual_refresh_flag(self, app):
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

        assert result["data"]["allow_manual_refresh"] is True


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
            execute_tineye_search("analysis-123", context, force_refresh=True)
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
                filtered_match_count=0,
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
            result = execute_tineye_search("analysis-123", context, force_refresh=True)

        assert result["status"] == "ERROR"
        assert "API request failed" in result["summary"]
        assert result["data"]["allow_manual_refresh"] is False


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

        def mock_execute(analysis_id, context, **kwargs):
            return {
                "status": "NOT FOUND",
                "summary": "No matches found.",
                "data": {"total_matches": 0, "matches": [], "buckets": {"oldest": [], "newest": [], "shame_list": []}},
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

        def mock_execute(analysis_id, context, **kwargs):
            return {
                "status": "NOT FOUND",
                "summary": "No matches found.",
                "data": {"total_matches": 0, "matches": [], "buckets": {"oldest": [], "newest": [], "shame_list": []}},
            }

        monkeypatch.setattr(routes, "execute_tineye_search", mock_execute)

        client = app.test_client()

        for i in range(5):
            response = client.post("/analysis/test-id/tineye/run")
            assert response.status_code == 200, f"Request {i+1} failed unexpectedly"

        response = client.post("/analysis/test-id/tineye/run")
        assert response.status_code == 429


class TestTinEyeTemplates:
    def test_tineye_template_renders_waiting_state(self, app):
        from flask import render_template

        with app.test_request_context():
            row = {
                "status": "WAITING",
                "summary": "Manual check required",
                "data": {"matches": []},
                "context": {"analysis_id": "test-123", "link_target": "_blank"},
            }
            html = render_template("partials/analyzers/tineye.html", row=row)
            assert "Check TinEye" in html
            assert "tineye/run" in html

    def test_tineye_template_renders_found_state(self, app):
        from flask import render_template

        with app.test_request_context():
            row = {
                "status": "FOUND",
                "summary": "10 matches found",
                "data": {"matches": [], "buckets": {"oldest": [], "newest": [], "shame_list": []}},
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
                "data": {"matches": [], "buckets": {"oldest": [], "newest": [], "shame_list": []}},
                "context": {"analysis_id": "test-123", "link_target": "_blank"},
            }
            html = render_template("partials/analyzers/tineye.html", row=row)
            assert "Something went wrong" in html
            assert "Check TinEye" in html

    def test_tineye_template_renders_stale_state(self, app):
        from flask import render_template

        with app.test_request_context():
            row = {
                "status": "STALE",
                "summary": "10 matches found",
                "data": {"matches": [], "buckets": {"oldest": [], "newest": [], "shame_list": []}},
                "context": {"analysis_id": "test-123", "link_target": "_blank"},
            }
            html = render_template("partials/analyzers/tineye.html", row=row)
            assert "Refresh TinEye" in html
            assert "outdated" in html

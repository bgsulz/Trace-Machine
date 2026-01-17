from types import SimpleNamespace

from veracity import db
from veracity.analyzers.context import AnalysisContext
from veracity.analyzers.tineye import get_tineye_status
from veracity.models import ImageRegistry


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
        assert result["data"]["allow_manual_refresh"] is True

    def test_ignores_neighbors(self, app):
        """TinEye status ignores neighbors - it always returns WAITING
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

        assert result["status"] == "WAITING"
        assert result["data"]["allow_manual_refresh"] is True


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

        monkeypatch.setattr(routes, "call_tineye_api", mock_call_api)
        monkeypatch.setattr(routes, "process_tineye_response", mock_process_response)
        monkeypatch.setattr(routes, "get_shame_list_matchers", mock_get_matchers)

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

        monkeypatch.setattr(routes, "call_tineye_api", mock_call_api)
        monkeypatch.setattr(routes, "process_tineye_response", mock_process_response)
        monkeypatch.setattr(routes, "get_shame_list_matchers", mock_get_matchers)

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
                "data": {},
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
            assert "Something went wrong" in html
            assert "Check TinEye" in html

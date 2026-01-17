import json
from types import SimpleNamespace

import requests

from veracity import db
from veracity.analyzers.context import AnalysisContext
from veracity.analyzers import synthid
from veracity.analyzers.synthid import get_synthid_status, execute_synthid_search
from veracity.models import ImageRegistry, ProvenanceFact


def _make_neighbor(*, detected=True, badge_text="Neighbor badge"):
    fact_payload = {
        "detected": detected,
        "badge_text": badge_text,
        "summary": "Neighbor summary",
        "matches": [],
    }
    fact = SimpleNamespace(analyzer="synthid", data=json.dumps(fact_payload))
    source = SimpleNamespace(url="https://example.com/source.png")
    return SimpleNamespace(
        phash="0011ffaa0011ffaa",
        whash="0011ffaa0011ffbb",
        facts=[fact],
        sources=[source],
    )


def _make_context(registry_id: int, *, neighbors=None):
    return AnalysisContext(
        image_bytes=b"payload",
        phash="0011ffaa0011ffaa",
        whash="0011ffaa0011ffbb",
        registry_id=registry_id,
        neighbors=neighbors or [],
        width=12,
        height=12,
    )


def _create_registry():
    registry = ImageRegistry(phash="0011ffaa0011ffaa", whash="0011ffaa0011ffbb")
    db.session.add(registry)
    db.session.commit()
    return registry


def test_get_synthid_status_returns_waiting_with_matches(app):
    neighbor = _make_neighbor()
    context = _make_context(registry_id=999, neighbors=[neighbor])

    with app.app_context():
        result = get_synthid_status(context)

    assert result["status"] == "WAITING"
    matches = result["data"]["matches"]
    assert len(matches) == 1
    assert matches[0]["badge"] == "Neighbor badge"


def test_get_synthid_status_returns_cached_fact_and_refreshes_matches(app):
    neighbor = _make_neighbor(badge_text="Updated badge")

    with app.app_context():
        registry = _create_registry()
        fact_payload = {
            "detected": True,
            "badge_text": "Original badge",
            "summary": "SynthID detected",
            "matches": [],
        }
        fact = ProvenanceFact(
            image_id=registry.id,
            analyzer="synthid",
            data=json.dumps(fact_payload),
        )
        db.session.add(fact)
        db.session.commit()

        context = _make_context(registry_id=registry.id, neighbors=[neighbor])
        result = get_synthid_status(context)

    assert result["status"] == "FOUND"
    assert result["data"]["badge_text"] == "Original badge"
    # Matches should be refreshed from neighbor data
    assert result["data"]["matches"]
    assert result["data"]["matches"][0]["badge"] == "Updated badge"


def test_execute_synthid_search_localhost_uses_mock_response(app, monkeypatch):
    with app.app_context():
        registry = _create_registry()
        context = _make_context(registry_id=registry.id)
        app.config["SYNTHID_MOCK_MODE"] = True

    monkeypatch.setattr(
        synthid,
        "url_for",
        lambda *args, **kwargs: "http://127.0.0.1/analysis/abc/raw",
    )

    with app.app_context():
        result = execute_synthid_search("analysis-id", context)
        facts = ProvenanceFact.query.filter_by(
            image_id=registry.id, analyzer="synthid"
        ).all()

    assert result["status"] == "FOUND"
    assert result["summary"] == "Made with Google AI"
    assert result["data"]["detected"] is True
    assert len(facts) == 1


def test_execute_synthid_search_remote_missing_api_key_errors(app, monkeypatch):
    with app.app_context():
        registry = _create_registry()
        context = _make_context(registry_id=registry.id)
        app.config["SYNTHID_MOCK_MODE"] = False

    monkeypatch.delenv("SERPAPI_KEY", raising=False)
    monkeypatch.setattr(
        synthid, "url_for", lambda *args, **kwargs: "https://example.com/image.png"
    )

    with app.app_context():
        result = execute_synthid_search("analysis-id", context)

    assert result["status"] == "ERROR"
    assert "missing SERPAPI_KEY" in result["summary"]


def test_execute_synthid_search_remote_api_failure_returns_error(
    app, monkeypatch
):
    with app.app_context():
        registry = _create_registry()
        context = _make_context(registry_id=registry.id)
        app.config["SYNTHID_MOCK_MODE"] = False

    monkeypatch.setenv("SERPAPI_KEY", "secret-key")
    monkeypatch.setattr(
        synthid, "url_for", lambda *args, **kwargs: "https://example.com/image.png"
    )

    def _failing_get(*_, **__):
        raise requests.RequestException("boom")

    monkeypatch.setattr(synthid.requests, "get", _failing_get)

    with app.app_context():
        result = execute_synthid_search("analysis-id", context)
        fact_count = ProvenanceFact.query.count()

    assert result["status"] == "ERROR"
    assert result["summary"] == "External API failed"
    assert fact_count == 0


class TestRunSynthIDRoute:
    """SynthID route is temporarily disabled and returns 404."""

    def test_run_synthid_route_returns_404(self, app):
        """SynthID route is disabled and returns 404 for all requests."""
        client = app.test_client()
        response = client.post("/analysis/nonexistent-id/synthid/run")
        assert response.status_code == 404

    def test_run_synthid_route_disabled_even_with_valid_analysis(self, app, monkeypatch):
        """SynthID route returns 404 even for valid analysis IDs."""
        from veracity import routes
        from io import BytesIO
        from PIL import Image

        img = Image.new("RGB", (100, 100), color=(0, 255, 0))
        buf = BytesIO()
        img.save(buf, format="PNG")
        image_bytes = buf.getvalue()

        monkeypatch.setattr(
            routes,
            "load_analysis_payload",
            lambda aid: (image_bytes, {"mime_type": "image/png", "source": "file"}),
        )

        client = app.test_client()
        response = client.post("/analysis/test-id/synthid/run")
        assert response.status_code == 404

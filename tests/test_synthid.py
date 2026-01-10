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
    assert "Mocked" in result["summary"]
    assert result["data"]["detected"] is True
    assert len(facts) == 1


def test_execute_synthid_search_remote_missing_api_key_errors(app, monkeypatch):
    with app.app_context():
        registry = _create_registry()
        context = _make_context(registry_id=registry.id)

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

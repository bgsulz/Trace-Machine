import json

import pytest

from veracity.analyzers import AnalyzerSpec, run_all_analyzers
from conftest import _make_test_image_bytes
from veracity.analyzers.human import run_human_consensus


@pytest.fixture(autouse=True)
def _push_app_context(app):
    with app.app_context():
        yield


def test_run_all_analyzers_preserves_order():
    events = []

    def make_spec(name: str) -> AnalyzerSpec:
        def _fn(_: bytes):
            events.append(name)
            return "OK", f"details-{name}"

        return AnalyzerSpec(name=name, func=_fn)

    analyzers = [make_spec("A"), make_spec("B"), make_spec("C")]

    results = run_all_analyzers(b"payload", analyzers)

    assert [row["name"] for row in results] == [spec.name for spec in analyzers]
    assert events == ["A", "B", "C"]


def test_run_all_analyzers_handles_exceptions():
    def ok(_: bytes):
        return "OK", "fine"

    def boom(_: bytes):  # pragma: no cover - executed in thread
        raise RuntimeError("boom")

    analyzers = [
        AnalyzerSpec(name="Good", func=ok),
        AnalyzerSpec(name="Bad", func=boom),
    ]

    results = run_all_analyzers(b"payload", analyzers)

    result_map = {row["name"]: row for row in results}
    assert result_map["Good"]["status"] == "OK"
    assert result_map["Bad"]["status"] == "ERROR"
    assert "boom" in result_map["Bad"]["details"]


def test_c2pa_analyzer_not_available(monkeypatch):
    # Simulate missing c2pa dependency
    from veracity.analyzers import c2pa as c2pa_analyzer

    monkeypatch.setattr(c2pa_analyzer, "Reader", None)
    result = c2pa_analyzer.run_c2pa(_make_test_image_bytes())
    assert result["status"] == "NOT AVAILABLE"
    assert "not installed" in str(result["summary"]).lower()


def test_c2pa_analyzer_writes_signer(monkeypatch):
    class DummyReader:
        def __init__(self, mime_type, stream):  # noqa: D401
            self._json = json.dumps(
                {
                    "manifests": {
                        "m1": {
                            "signature_info": {"issuer": "Adobe"},
                            "claim_generator": "Photoshop",
                        }
                    },
                    "active_manifest": "m1",
                }
            )

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: D401
            return False

        def json(self):
            return self._json

    from veracity.analyzers import c2pa as c2pa_analyzer

    monkeypatch.setattr(c2pa_analyzer, "Reader", DummyReader)
    result = c2pa_analyzer.run_c2pa(_make_test_image_bytes())
    assert result["status"] == "FOUND"
    assert "Adobe" in str(result["summary"])


def test_human_consensus_returns_phash():
    result = run_human_consensus(_make_test_image_bytes())
    assert result["status"] == "NO DATA"
    assert "phash" in result["data"]

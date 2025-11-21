import json

import veracity.analyzers as analyzers
from veracity.analyzers import AnalyzerSpec, run_all_analyzers
from conftest import _make_test_image_bytes


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
    monkeypatch.setattr(analyzers, "Reader", None)
    status, details = analyzers._digital_signature_c2pa(_make_test_image_bytes())
    assert status == "NOT AVAILABLE"
    assert "not installed" in details.lower()


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

    monkeypatch.setattr(analyzers, "Reader", DummyReader)
    status, details = analyzers._digital_signature_c2pa(_make_test_image_bytes())
    assert status == "FOUND"
    assert "Adobe" in details


def test_human_consensus_returns_phash():
    status, details = analyzers._human_consensus_phash(_make_test_image_bytes())
    assert status == "HASHED"
    assert "phash=" in details

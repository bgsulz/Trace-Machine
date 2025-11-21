import json
from io import BytesIO

import pytest
import imagehash
from PIL import Image

from veracity import db
from veracity.analyzers import AnalyzerSpec, run_all_analyzers
from veracity.analyzers.human import run_human_consensus
from veracity.models import ImageConsensus
from conftest import _make_test_image_bytes


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
    data = result["data"]
    assert "phash" in data
    assert data["matches"] == []
    assert (data["totals"].get("total_votes") or 0) == 0


def test_human_consensus_uses_fuzzy_match(app):
    image_bytes = _make_test_image_bytes()

    # Compute the target hash exactly as the analyzer does
    with Image.open(BytesIO(image_bytes)) as img:
        target_hash = imagehash.phash(img)
    target_hex = str(target_hash)

    # Flip a single bit to create a nearby hash with small Hamming distance
    arr = target_hash.hash.copy()
    arr[0, 0] = ~arr[0, 0]
    fuzzy_hash = imagehash.ImageHash(arr)
    fuzzy_hex = str(fuzzy_hash)

    # Seed the DB with votes for the fuzzy hash
    with app.app_context():
        row = ImageConsensus(phash=fuzzy_hex, vote_real=3, vote_ai=7)
        db.session.add(row)
        db.session.commit()

        result = run_human_consensus(image_bytes)

    assert result["status"] == "FOUND"
    data = result["data"]
    assert data["phash"] == target_hex
    matches = data["matches"]
    assert len(matches) == 1
    match = matches[0]
    assert match["phash"] == fuzzy_hex
    assert match["vote_real"] == 3
    assert match["vote_ai"] == 7
    assert 0 < match["distance"] <= 4
    assert data["totals"]["vote_real"] == 3
    assert data["totals"]["vote_ai"] == 7

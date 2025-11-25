import json
from io import BytesIO

import pytest
import imagehash
from PIL import Image

from veracity.analyzers import AnalyzerSpec, run_all_analyzers
from veracity.analyzers.human import run_human_consensus
from veracity.analyzers.manager import AnalysisContext
from conftest import _make_test_image_bytes


@pytest.fixture(autouse=True)
def _push_app_context(app):
    with app.app_context():
        yield


def test_run_all_analyzers_preserves_order():
    events = []

    def make_spec(name: str, slug: str) -> AnalyzerSpec:
        def _fn(_context: AnalysisContext):
            events.append(name)
            return {
                "status": "OK",
                "summary": f"details-{name}",
                "data": {},
            }

        return AnalyzerSpec(name=name, slug=slug, func=_fn)

    analyzers = [make_spec("A", "a"), make_spec("B", "b"), make_spec("C", "c")]

    context = AnalysisContext(
        image_bytes=b"payload",
        phash="deadbeefdeadbeef",
        registry_id=1,
        neighbors=[],
    )
    results = run_all_analyzers(context, analyzers)

    assert [row["name"] for row in results] == [spec.name for spec in analyzers]
    assert [row["slug"] for row in results] == [spec.slug for spec in analyzers]
    assert events == ["A", "B", "C"]


def test_run_all_analyzers_handles_exceptions():
    def ok(_context: AnalysisContext):
        return {
            "status": "OK",
            "summary": "fine",
            "data": {},
        }

    def boom(_context: AnalysisContext):  # pragma: no cover - executed in thread
        raise RuntimeError("boom")

    analyzers = [
        AnalyzerSpec(name="Good", slug="good", func=ok),
        AnalyzerSpec(name="Bad", slug="bad", func=boom),
    ]

    context = AnalysisContext(
        image_bytes=b"payload",
        phash="deadbeefdeadbeef",
        registry_id=1,
        neighbors=[],
    )
    results = run_all_analyzers(context, analyzers)

    result_map = {row["name"]: row for row in results}
    assert result_map["Good"]["status"] == "OK"
    assert result_map["Bad"]["status"] == "ERROR"
    assert "boom" in result_map["Bad"]["details"]


def test_c2pa_analyzer_not_available(monkeypatch):
    # Simulate missing c2pa dependency
    from veracity.analyzers import c2pa as c2pa_analyzer

    monkeypatch.setattr(c2pa_analyzer, "Reader", None)
    image_bytes = _make_test_image_bytes()
    context = AnalysisContext(
        image_bytes=image_bytes,
        phash="deadbeefdeadbeef",
        registry_id=1,
        neighbors=[],
    )
    result = c2pa_analyzer.run_c2pa(context)
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
    image_bytes = _make_test_image_bytes()
    context = AnalysisContext(
        image_bytes=image_bytes,
        phash="deadbeefdeadbeef",
        registry_id=1,
        neighbors=[],
    )
    result = c2pa_analyzer.run_c2pa(context)
    assert result["status"] == "FOUND"
    assert "Adobe" in str(result["summary"])


def test_human_consensus_returns_phash():
    image_bytes = _make_test_image_bytes()

    # Compute a realistic perceptual hash for the image
    with Image.open(BytesIO(image_bytes)) as img:
        target_hash = imagehash.phash(img)
    target_hex = str(target_hash)

    context = AnalysisContext(
        image_bytes=image_bytes,
        phash=target_hex,
        registry_id=1,
        neighbors=[],
    )

    result = run_human_consensus(context)
    assert result["status"] == "NO DATA"
    data = result["data"]
    assert "phash" in data
    assert data["matches"] == []
    assert (data["totals"].get("total_votes") or 0) == 0
    assert data["has_matches"] is False
    assert data["no_votes_message"]


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

    class Neighbor:
        def __init__(self, phash: str, vote_real: int, vote_ai: int):
            self.phash = phash
            # Simple object with the fields the analyzer needs
            self.consensus = type("Consensus", (), {
                "vote_real": vote_real,
                "vote_edited": 0,
                "vote_ai": vote_ai,
            })()
            self.created_at = None
            self.sources = []

    neighbor = Neighbor(phash=fuzzy_hex, vote_real=3, vote_ai=7)

    context = AnalysisContext(
        image_bytes=image_bytes,
        phash=target_hex,
        registry_id=1,
        neighbors=[neighbor],
    )

    result = run_human_consensus(context)

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
    assert data["has_matches"] is True
    assert "Showing" in data["matches_summary"]


def test_human_consensus_attaches_sources(app):
    image_bytes = _make_test_image_bytes()

    # Compute the target hash exactly as the analyzer does
    with Image.open(BytesIO(image_bytes)) as img:
        target_hash = imagehash.phash(img)

    # Flip a single bit to create a nearby hash with small Hamming distance
    arr = target_hash.hash.copy()
    arr[0, 0] = ~arr[0, 0]
    fuzzy_hash = imagehash.ImageHash(arr)
    fuzzy_hex = str(fuzzy_hash)

    class Source:
        def __init__(self, url: str) -> None:
            self.url = url

    class Neighbor:
        def __init__(self, phash: str):
            self.phash = phash
            self.consensus = type("Consensus", (), {
                "vote_real": 1,
                "vote_edited": 0,
                "vote_ai": 2,
            })()
            self.created_at = None
            self.sources = [
                Source("https://example.com/a.png"),
                Source("https://example.com/b.png"),
            ]

    neighbor = Neighbor(phash=fuzzy_hex)

    context = AnalysisContext(
        image_bytes=image_bytes,
        phash=str(target_hash),
        registry_id=1,
        neighbors=[neighbor],
    )

    result = run_human_consensus(context)

    assert result["status"] == "FOUND"
    data = result["data"]
    matches = data["matches"]
    assert len(matches) == 1
    match = matches[0]
    sources = match.get("sources")
    assert isinstance(sources, list)
    assert len(sources) == 2
    urls = {entry["url"] for entry in sources}
    assert urls == {"https://example.com/a.png", "https://example.com/b.png"}

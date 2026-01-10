import json
from io import BytesIO
from types import SimpleNamespace

import pytest
import imagehash
from PIL import Image, PngImagePlugin

from veracity.analyzers import AnalyzerSpec, run_all_analyzers
from veracity.analyzers.human import (
    _build_vote_breakdown,
    _MAX_HAMMING_DISTANCE,
    run_human_consensus,
)
from veracity.analyzers.exif import run_exif_metadata, _collect_text_chunks
from veracity.analyzers.manager import AnalysisContext
from veracity.analyzers import c2pa as c2pa_analyzer
from veracity.analyzers.c2pa import _detect_mime_type, _run_c2pa_tool
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
        whash="feedfacefeedface",
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
        whash="feedfacefeedface",
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
        whash="feedfacefeedface",
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
        whash="feedfacefeedface",
        registry_id=1,
        neighbors=[],
    )
    result = c2pa_analyzer.run_c2pa(context)
    assert result["status"] == "FOUND"
    assert "Adobe" in str(result["summary"])


def test_run_c2pa_tool_returns_error_on_reader_failure(monkeypatch):
    class FailingReader:
        def __init__(self, mime_type, stream):  # noqa: D401, ARG002
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: D401, ARG002
            return False

        def json(self):
            raise RuntimeError("unexpected parser failure")

    monkeypatch.setattr(c2pa_analyzer, "Reader", FailingReader)
    image_bytes = _make_test_image_bytes()

    result = _run_c2pa_tool(image_bytes)

    assert result["status"] == "ERROR"
    assert "unexpected parser failure" in result["summary"]


def test_human_consensus_returns_phash():
    image_bytes = _make_test_image_bytes()

    # Compute a realistic perceptual hash for the image
    with Image.open(BytesIO(image_bytes)) as img:
        target_hash = imagehash.phash(img)
        target_whash = imagehash.whash(img)
    target_hex = str(target_hash)
    target_whash_hex = str(target_whash)

    context = AnalysisContext(
        image_bytes=image_bytes,
        phash=target_hex,
        whash=target_whash_hex,
        registry_id=1,
        neighbors=[],
        width=12,
        height=12,
    )

    result = run_human_consensus(context)
    assert result["status"] == "NOT FOUND"
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
        target_whash = imagehash.whash(img)
    target_hex = str(target_hash)
    target_whash_hex = str(target_whash)

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
        whash=target_whash_hex,
        registry_id=1,
        neighbors=[neighbor],
        width=12,
        height=12,
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
    assert "Similar" in data["matches_summary"]


def test_human_consensus_attaches_sources(app):
    image_bytes = _make_test_image_bytes()

    # Compute the target hash exactly as the analyzer does
    with Image.open(BytesIO(image_bytes)) as img:
        target_hash = imagehash.phash(img)
        target_whash = imagehash.whash(img)

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
        whash=str(target_whash),
        registry_id=1,
        neighbors=[neighbor],
        width=12,
        height=12,
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


def test_human_consensus_reports_threshold_and_overall_breakdown():
    class Neighbor:
        def __init__(self, phash, whash, real, edited, ai):
            self.phash = phash
            self.whash = whash
            self.consensus = SimpleNamespace(
                vote_real=real,
                vote_edited=edited,
                vote_ai=ai,
            )
            self.created_at = None
            self.sources = []

    neighbors = [
        Neighbor("0011ffaa0011ffaa", "0011ffaa0011ffab", 2, 1, 0),
        Neighbor("0011ffaa0011ffbb", "0011ffaa0011ffcc", 0, 1, 3),
    ]

    context = AnalysisContext(
        image_bytes=b"payload",
        phash="0011ffaa0011ffaa",
        whash="0011ffaa0011ffbb",
        registry_id=1,
        neighbors=neighbors,
        width=12,
        height=12,
    )

    result = run_human_consensus(context)
    data = result["data"]

    assert data["threshold"] == _MAX_HAMMING_DISTANCE
    totals = data["totals"]
    breakdown = data["overall_breakdown"]
    counts = breakdown["counts"]

    assert counts["real"] == totals["vote_real"]
    assert counts["edited"] == totals["vote_edited"]
    assert counts["ai"] == totals["vote_ai"]
    assert breakdown["total"] == totals["total_votes"]


def _make_png_with_text(chunks: dict[str, str]) -> bytes:
    img = Image.new("RGB", (12, 12), color=(0, 128, 255))
    pnginfo = PngImagePlugin.PngInfo()
    for key, value in chunks.items():
        pnginfo.add_text(key, value)
    buf = BytesIO()
    img.save(buf, format="PNG", pnginfo=pnginfo)
    return buf.getvalue()


def test_exif_detects_automatic1111_metadata():
    sample = (
        "Astronaut in a jungle, cold color palette, muted colors, detailed, 8k "
        "Steps: 50, Sampler: DPM++ 2M Karras, CFG scale: 5, Seed: 42, Size: 1024x1024, "
        "Model hash: 1f69731261, Model: sd_xl_base_0.9, Clip skip: 2, RNG: CPU, Version: v1.4.1"
    )
    image_bytes = _make_png_with_text({"parameters": sample})
    context = AnalysisContext(
        image_bytes=image_bytes,
        phash="deadbeefdeadbeef",
        whash="deadbeefdeadbeef",
        registry_id=1,
        neighbors=[],
        width=12,
        height=12,
    )

    result = run_exif_metadata(context)

    assert result["status"] == "FOUND"
    findings = result["data"]["findings"]
    assert len(findings) == 1
    finding = findings[0]
    assert finding["tool"] == "Automatic1111"
    assert finding["key"].lower() == "parameters"
    metadata = finding["metadata"]
    assert metadata.get("prompt", "").startswith("Astronaut in a jungle")
    assert metadata.get("steps") == "50"
    assert metadata.get("cfg_scale") == "5"
    assert metadata.get("seed") == "42"
    chunks = result["data"]["chunks"]
    assert "parameters" in chunks


def test_exif_detects_comfyui_prompt_and_workflow():
    prompt_payload = json.dumps({"5": {"inputs": {"text": "galaxy fox"}}})
    workflow_payload = json.dumps({"last_node_id": 27, "nodes": [{"id": 7}]})
    image_bytes = _make_png_with_text(
        {
            "prompt": prompt_payload,
            "workflow": workflow_payload,
        }
    )
    context = AnalysisContext(
        image_bytes=image_bytes,
        phash="cafebabecafebabe",
        whash="cafebabecafebabe",
        registry_id=1,
        neighbors=[],
        width=12,
        height=12,
    )

    result = run_exif_metadata(context)

    assert result["status"] == "FOUND"
    findings = result["data"]["findings"]
    assert len(findings) == 2
    kinds = {finding["metadata"].get("kind") for finding in findings}
    assert kinds == {"prompt", "workflow"}
    for finding in findings:
        parsed = finding["metadata"].get("parsed_json")
        assert isinstance(parsed, dict)
    chunks = result["data"]["chunks"]
    assert {"prompt", "workflow"}.issubset(chunks.keys())


def test_exif_returns_not_found_without_known_metadata():
    image_bytes = _make_test_image_bytes()
    context = AnalysisContext(
        image_bytes=image_bytes,
        phash="0011ffaa0011ffaa",
        whash="0011ffaa0011ffaa",
        registry_id=1,
        neighbors=[],
        width=12,
        height=12,
    )

    result = run_exif_metadata(context)

    assert result["status"] == "NOT FOUND"
    assert result["data"]["findings"] == []
    chunks = result["data"]["chunks"]
    assert chunks  # should expose all raw metadata even without detections
    assert chunks["FileType"] == "PNG"
    assert chunks["ImageSize"] == "10x10"
    assert chunks["ColorMode"] == "RGB"


def test_collect_text_chunks_includes_numeric_and_exif_values():
    class DummyImage:
        def __init__(self):
            self.info = {
                "BitDepth": 8,
                "ExtraTuple": (1, 2, 3),
                "BinaryPayload": b"abc",
            }

        def getexif(self):
            return {
                40962: 2048,  # PixelXDimension
                40963: 1024,  # PixelYDimension
            }

    chunks = _collect_text_chunks(DummyImage())

    assert chunks["BitDepth"] == "8"
    assert chunks["ExtraTuple"] == "1, 2, 3"
    assert chunks["BinaryPayload"] == "abc"


def test_detect_mime_type_identifies_avif_signature():
    # Bytes 4:8 should be 'ftyp' and 8:12 a known AVIF brand.
    payload = b"\x00\x00\x00\x00ftypavif" + b"\x00" * 20
    assert _detect_mime_type(payload) == "image/avif"


def test_run_c2pa_sets_similar_when_neighbors_have_facts(monkeypatch):
    context = AnalysisContext(
        image_bytes=b"payload",
        phash="0011ffaa0011ffaa",
        whash="0011ffaa0011ffbb",
        registry_id=42,
        neighbors=[
            SimpleNamespace(
                phash="0011ffaa0011ffab",
                whash="0011ffaa0011ffac",
                facts=[
                    SimpleNamespace(analyzer="c2pa", data="Signed by Example Corp")
                ],
                sources=[SimpleNamespace(url="https://example.com/img")],
            )
        ],
        width=12,
        height=12,
    )

    monkeypatch.setattr(
        c2pa_analyzer,
        "_run_c2pa_tool",
        lambda *_: {
            "status": "NOT FOUND",
            "summary": "No C2PA signature found.",
            "data": {},
        },
    )

    result = c2pa_analyzer.run_c2pa(context)

    assert result["status"] == "SIMILAR"
    assert "visually similar images" in result["summary"]
    matches = result["data"]["matches"]
    assert len(matches) == 1
    assert matches[0]["fact_data"] == "Signed by Example Corp"


def test_build_vote_breakdown_handles_zero_totals():
    breakdown = _build_vote_breakdown(real=0, edited=0, ai=0)
    assert breakdown["total"] == 0
    for segment in breakdown["segments"]:
        assert segment["count"] == 0
        assert segment["percent"] == 0

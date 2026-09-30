import json
from io import BytesIO
from types import SimpleNamespace

import pytest
import imagehash
from flask import current_app
from PIL import Image, PngImagePlugin

from veracity import db
from veracity.analyzers import AnalyzerSpec, run_all_analyzers
from veracity.analyzers.human import (
    _build_vote_breakdown,
    _MAX_HAMMING_DISTANCE,
    run_human_consensus,
)
from veracity.analyzers.exif import run_exif_metadata, _collect_text_chunks
from veracity.analyzers.invisible import DecoderUnavailable, run_invisible_watermarks
from veracity.analyzers.manager import AnalysisContext
from veracity.analyzers import c2pa as c2pa_analyzer
from veracity.analyzers.c2pa import _detect_mime_type, _run_c2pa_tool
from veracity.models import ImageRegistry, ProvenanceFact
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


def test_run_c2pa_tool_extracts_rich_manifest_fields(monkeypatch):
    manifest = {
        "manifests": {
            "m1": {
                "title": "Google image",
                "format": "image/jpeg",
                "instance_id": "urn:uuid:abc123",
                "signature_info": {
                    "issuer": "Google LLC",
                    "alg": "es256",
                    "time": "2026-01-02T03:04:05Z",
                },
                "claim_generator": "Google AI Studio",
                "claim_generator_info": [
                    {"name": "imagen", "version": "3.0"},
                ],
                "assertions": [
                    {
                        "label": "c2pa.actions",
                        "data": {
                            "actions": [
                                {
                                    "action": "c2pa.created",
                                    "digitalSourceType": "http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia",
                                    "softwareAgent": {
                                        "name": "Imagen",
                                        "version": "3.0",
                                    },
                                }
                            ]
                        },
                    },
                    {
                        "label": "stds.exif",
                        "data": {
                            "Make": "Google",
                            "Model": "Pixel 9",
                            "Software": "Camera 9.0",
                        },
                    },
                ],
                "ingredients": [
                    {
                        "title": "base image",
                        "relationship": "parentOf",
                        "format": "image/jpeg",
                        "validation_status": ["claimSignature.validated"],
                    }
                ],
            }
        },
        "active_manifest": "m1",
        "validation_status": ["claimSignature.validated"],
    }

    class DummyReader:
        def __init__(self, mime_type, stream):  # noqa: D401, ARG002
            self._json = json.dumps(manifest)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: D401, ARG002
            return False

        def json(self):
            return self._json

    monkeypatch.setattr(c2pa_analyzer, "Reader", DummyReader)

    result = _run_c2pa_tool(_make_test_image_bytes())

    assert result["status"] == "FOUND"
    assert result["summary"] == "Signed by Google LLC (Generative AI signal)"

    data = result["data"]
    assert data["signer"] == "Google LLC"
    assert data["tool"] == "Google AI Studio"
    assert data["signature_status"] == "valid"
    assert data["origin_signals"] == ["ai"]
    assert data["origin_label"] == "Generative AI signal"
    assert data["claim_generator_info"] == ["imagen 3.0"]
    assert data["capture_details"]["make"] == "Google"
    assert data["capture_details"]["model"] == "Pixel 9"
    assert data["capture_details"]["software"] == "Camera 9.0"

    actions = data["actions"]
    assert len(actions) == 1
    assert actions[0]["action"] == "c2pa.created"
    assert actions[0]["action_display"] == "Created"
    assert actions[0]["origin_signal"] == "ai"
    assert actions[0]["software_agent"] == "Imagen 3.0"

    assert data["manifest_label"] == "m1"
    assert data["manifest_title"] == "Google image"
    assert data["manifest_count"] == 1
    assert data["raw_manifest_store"]["active_manifest"] == "m1"


def test_c2pa_analyzer_handles_duplicate_fact(monkeypatch, app):
    image = ImageRegistry(phash="deadbeefdeadbeef", whash="feedfacefeedface")
    db.session.add(image)
    db.session.commit()

    fact = ProvenanceFact(
        image_id=image.id, analyzer="c2pa", data="Signed by OpenAI"
    )
    db.session.add(fact)
    db.session.commit()

    def _fake_tool(_image_bytes):
        return {"status": "FOUND", "summary": "Signed by OpenAI", "data": {}}

    monkeypatch.setattr(c2pa_analyzer, "_run_c2pa_tool", _fake_tool)

    context = AnalysisContext(
        image_bytes=_make_test_image_bytes(),
        phash=image.phash,
        whash=image.whash,
        registry_id=image.id,
        neighbors=[],
    )

    result = c2pa_analyzer.run_c2pa(context)

    assert result["status"] == "FOUND"
    assert (
        ProvenanceFact.query.filter_by(image_id=image.id, analyzer="c2pa").count()
        == 1
    )


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
    assert data["has_distant_matches"] is True
    assert "Similar" in data["matches_summary"]


def test_human_consensus_treats_self_votes_as_direct_only():
    neighbor = SimpleNamespace(
        id=42,
        phash="0011ffaa0011ffaa",
        whash="0011ffaa0011ffbb",
        consensus=SimpleNamespace(vote_real=2, vote_edited=0, vote_ai=1),
        created_at=None,
        sources=[],
    )

    context = AnalysisContext(
        image_bytes=b"payload",
        phash="0011ffaa0011ffaa",
        whash="0011ffaa0011ffbb",
        registry_id=42,
        neighbors=[neighbor],
        width=12,
        height=12,
    )

    result = run_human_consensus(context)
    data = result["data"]

    assert result["status"] == "FOUND"
    assert data["has_matches"] is True
    assert data["has_distant_matches"] is False
    assert data["distant_match_count"] == 0
    assert data["matches_summary"] == ""
    assert data["totals"]["total_votes"] == 3
    assert "direct vote" in result["summary"]


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


def test_human_consensus_exposes_local_match_evidence():
    neighbor = SimpleNamespace(
        phash="0011ffaa0011ffaa",
        whash="0011ffaa0011ffbb",
        consensus=SimpleNamespace(vote_real=2, vote_edited=0, vote_ai=1),
        created_at=None,
        sources=[],
        match_method="local",
        local_match=SimpleNamespace(
            extractor="akaze",
            good_matches=30,
            inliers=22,
            inlier_ratio=0.73,
            homography_found=True,
            crop_box=(0.1, 0.2, 0.5, 0.4),
        ),
    )

    context = AnalysisContext(
        image_bytes=b"payload",
        phash="f0f0f0f0f0f0f0f0",
        whash="0f0f0f0f0f0f0f0f",
        registry_id=1,
        neighbors=[neighbor],
        width=12,
        height=12,
    )

    result = run_human_consensus(context)
    data = result["data"]
    assert data["local_match_count"] == 1
    assert len(data["matches"]) == 1

    match = data["matches"][0]
    assert match["match_method"] == "local"
    assert match["distance"] is None
    assert match["local"]["extractor"] == "akaze"
    assert match["local"]["inliers"] == 22
    assert match["local"]["crop_box"] == (0.1, 0.2, 0.5, 0.4)


def _make_png_with_text(chunks: dict[str, str]) -> bytes:
    img = Image.new("RGB", (12, 12), color=(0, 128, 255))
    pnginfo = PngImagePlugin.PngInfo()
    for key, value in chunks.items():
        pnginfo.add_text(key, value)
    buf = BytesIO()
    img.save(buf, format="PNG", pnginfo=pnginfo)
    return buf.getvalue()


def _make_png_with_suffix(suffix: bytes) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (12, 12), color=(0, 128, 255)).save(buf, format="PNG")
    return buf.getvalue() + suffix


def _make_jpeg_with_exif(tags: dict[int, str]) -> bytes:
    exif = Image.Exif()
    for key, value in tags.items():
        exif[key] = value
    buf = BytesIO()
    Image.new("RGB", (12, 12), color=(0, 128, 255)).save(
        buf, format="JPEG", exif=exif
    )
    return buf.getvalue()


def _make_analysis_context(image_bytes: bytes) -> AnalysisContext:
    return AnalysisContext(
        image_bytes=image_bytes,
        phash="deadbeefdeadbeef",
        whash="deadbeefdeadbeef",
        registry_id=1,
        neighbors=[],
        width=12,
        height=12,
    )


def _finding_tools(result: dict[str, object]) -> set[str]:
    return {
        finding["tool"]
        for finding in result["data"]["findings"]  # type: ignore[index]
    }


def test_exif_analyzer_renamed_but_slug_is_stable():
    from veracity.analyzers.manager import get_analyzer_spec

    spec = get_analyzer_spec("exif")

    assert spec is not None
    assert spec.slug == "exif"
    assert spec.name == "AI Metadata (EXIF/XMP/IPTC)"


def test_portal_analyzer_renamed_but_slug_is_stable():
    from veracity.analyzers.manager import get_analyzer_spec

    spec = get_analyzer_spec("synthid")

    assert spec is not None
    assert spec.slug == "synthid"
    assert spec.name == "Verification Portals"


def test_invisible_analyzer_registered():
    from veracity.analyzers.manager import get_analyzer_spec

    spec = get_analyzer_spec("invisible")

    assert spec is not None
    assert spec.name == "Invisible Watermarks"


def test_invisible_analyzer_only_active_when_configured(app):
    from veracity.analyzers.manager import get_active_analyzers

    with app.app_context():
        assert "invisible" not in {spec.slug for spec in get_active_analyzers()}

        app.config["INVISIBLE_WATERMARK_DECODERS"] = {"open_dwt_dct"}

        assert "invisible" in {spec.slug for spec in get_active_analyzers()}


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


def test_collect_text_chunks_normalizes_top_level_exif_tags():
    image_bytes = _make_jpeg_with_exif(
        {
            270: "Signature: " + "A" * 90,  # ImageDescription
            271: "Ideogram AI",  # Make
            305: "Adobe Firefly",  # Software
            315: "123e4567-e89b-12d3-a456-426614174000",  # Artist
        }
    )

    with Image.open(BytesIO(image_bytes)) as img:
        chunks = _collect_text_chunks(img, image_bytes)

    assert chunks["ImageDescription"].startswith("Signature:")
    assert chunks["Make"] == "Ideogram AI"
    assert chunks["Software"] == "Adobe Firefly"
    assert chunks["Artist"] == "123e4567-e89b-12d3-a456-426614174000"
    assert "270" not in chunks
    assert "271" not in chunks
    assert "305" not in chunks
    assert "315" not in chunks


def test_exif_detects_generator_software_and_ignores_plain_editor():
    ai_result = run_exif_metadata(
        _make_analysis_context(_make_jpeg_with_exif({305: "Adobe Firefly"}))
    )
    editor_result = run_exif_metadata(
        _make_analysis_context(_make_jpeg_with_exif({305: "Adobe Photoshop 25.0"}))
    )

    assert ai_result["status"] == "FOUND"
    assert "Embedded generator tag" in _finding_tools(ai_result)
    assert editor_result["status"] == "NOT FOUND"


def test_exif_detects_generator_make_and_ignores_camera_make():
    ai_result = run_exif_metadata(
        _make_analysis_context(_make_jpeg_with_exif({271: "Ideogram AI"}))
    )
    camera_result = run_exif_metadata(
        _make_analysis_context(_make_jpeg_with_exif({271: "Apple"}))
    )

    assert ai_result["status"] == "FOUND"
    assert "Embedded generator tag" in _finding_tools(ai_result)
    assert camera_result["status"] == "NOT FOUND"


def test_exif_detects_novelai_png_text_chunks():
    image_bytes = _make_png_with_text(
        {
            "Software": "NovelAI",
            "Source": "NovelAI Diffusion V4.5 C02D4F98",
            "Title": "NovelAI generated image",
        }
    )

    result = run_exif_metadata(_make_analysis_context(image_bytes))

    assert result["status"] == "FOUND"
    assert "Embedded generator tag" in _finding_tools(result)


def test_exif_detects_tc260_aigc_png_chunk_and_xmp():
    chunk_bytes = _make_png_with_text(
        {
            "AIGC": json.dumps(
                {"Label": "1", "ContentProducer": "doubao", "ProduceID": "abc123"}
            )
        }
    )
    xmp = (
        b'<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        b'<rdf:Description xmlns:TC260="http://www.tc260.org.cn/ns/AIGC/1.0/">'
        b"<TC260:AIGC>{&quot;Label&quot;:&quot;1&quot;,&quot;ContentProducer&quot;:&quot;BYTEDANCE001&quot;}</TC260:AIGC>"
        b"</rdf:Description></rdf:RDF></x:xmpmeta>"
    )
    xmp_bytes = _make_png_with_suffix(xmp)

    chunk_result = run_exif_metadata(_make_analysis_context(chunk_bytes))
    xmp_result = run_exif_metadata(_make_analysis_context(xmp_bytes))

    assert "China AIGC label (TC260)" in _finding_tools(chunk_result)
    assert "China AIGC label (TC260)" in _finding_tools(xmp_result)


def test_exif_detects_iptc_digital_source_and_ai_system():
    xmp = (
        b'<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        b'<rdf:Description xmlns:Iptc4xmpExt="http://iptc.org/std/Iptc4xmpExt/2008-02-29/">'
        b"<Iptc4xmpExt:DigitalSourceType>trainedAlgorithmicMedia</Iptc4xmpExt:DigitalSourceType>"
        b"<Iptc4xmpExt:AISystemUsed>ChatGPT DALL-E</Iptc4xmpExt:AISystemUsed>"
        b"</rdf:Description></rdf:RDF></x:xmpmeta>"
    )
    image_bytes = _make_png_with_suffix(xmp)

    result = run_exif_metadata(_make_analysis_context(image_bytes))

    assert result["status"] == "FOUND"
    tools = _finding_tools(result)
    assert "IPTC AI source" in tools
    assert "IPTC AI disclosure" in tools


def test_exif_findings_without_parsed_json_render(client):
    """Regression: a finding with no parsed JSON (e.g. IPTC) crashed the fragment."""
    xmp = (
        b'<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        b'<rdf:Description xmlns:Iptc4xmpExt="http://iptc.org/std/Iptc4xmpExt/2008-02-29/">'
        b"<Iptc4xmpExt:DigitalSourceType>trainedAlgorithmicMedia</Iptc4xmpExt:DigitalSourceType>"
        b"</rdf:Description></rdf:RDF></x:xmpmeta>"
    )
    data = {"file": (BytesIO(_make_png_with_suffix(xmp)), "a.png"), "image_url": ""}
    resp = client.post("/analyze", data=data, content_type="multipart/form-data")
    analysis_id = resp.headers["Location"].rsplit("/", 1)[-1]

    fragment = client.get(f"/analysis/{analysis_id}/analyzers/exif")

    assert fragment.status_code == 200
    assert "trainedAlgorithmicMedia" in fragment.data.decode()


def test_graph_outline_tolerates_odd_node_ids():
    from veracity.web.ui import graph_outline

    node = {"class_type": "KSampler", "inputs": {"seed": 1}}
    outline = graph_outline({"2": node, "²": node, "9" * 5000: node, "10": node})
    assert [n["id"] for n in outline][:2] == ["2", "10"]


def test_exif_detects_xai_signature_pair_and_requires_both_fields():
    signature = "Signature: " + "A" * 120
    uuid = "123e4567-e89b-12d3-a456-426614174000"

    full_result = run_exif_metadata(
        _make_analysis_context(_make_jpeg_with_exif({270: signature, 315: uuid}))
    )
    partial_result = run_exif_metadata(
        _make_analysis_context(_make_jpeg_with_exif({270: signature}))
    )

    assert full_result["status"] == "FOUND"
    assert "xAI/Grok signature" in _finding_tools(full_result)
    assert partial_result["status"] == "NOT FOUND"


def test_exif_detects_huggingface_job_id():
    image_bytes = _make_png_with_text(
        {"hf-job-id": "123e4567-e89b-12d3-a456-426614174000"}
    )

    result = run_exif_metadata(_make_analysis_context(image_bytes))

    assert result["status"] == "FOUND"
    assert "HuggingFace job marker" in _finding_tools(result)


def test_exif_detects_samsung_genai_marker_with_container_gate():
    marked = _make_png_with_suffix(
        b' PhotoEditor_Re_Edit_Data {"genAIType": 4} '
    )
    ungated = _make_png_with_suffix(b' {"genAIType": 4} ')

    marked_result = run_exif_metadata(_make_analysis_context(marked))
    ungated_result = run_exif_metadata(_make_analysis_context(ungated))

    assert marked_result["status"] == "FOUND"
    assert "Samsung Galaxy AI marker" in _finding_tools(marked_result)
    assert ungated_result["status"] == "NOT FOUND"


def _enable_invisible_decoders(*decoders: str) -> None:
    current_app.config["INVISIBLE_WATERMARK_DECODERS"] = set(decoders)


def test_invisible_analyzer_default_disabled_even_if_decoders_exist(monkeypatch):
    from veracity.analyzers import invisible

    monkeypatch.setattr(
        invisible,
        "_detect_open_dwt_dct_watermark",
        lambda *_: {
            "label": "Open DWT-DCT watermark",
            "scheme": "Stable Diffusion XL",
        },
    )
    monkeypatch.setattr(
        invisible,
        "_detect_trustmark",
        lambda *_: {
            "label": "Adobe TrustMark",
            "scheme": "Adobe TrustMark variant P, schema 0",
        },
    )

    result = run_invisible_watermarks(_make_analysis_context(_make_test_image_bytes()))

    assert result["status"] == "NOT AVAILABLE"
    assert result["data"]["attempted_decoders"] == []
    assert set(result["data"]["disabled_decoders"]) == {"open_dwt_dct", "adobe_trustmark"}
    assert "decoders are enabled" in result["summary"]


def test_invisible_analyzer_not_available_without_optional_decoders(monkeypatch):
    from veracity.analyzers import invisible

    _enable_invisible_decoders("open_dwt_dct", "adobe_trustmark")
    monkeypatch.setattr(
        invisible,
        "_detect_open_dwt_dct_watermark",
        lambda *_: (_ for _ in ()).throw(DecoderUnavailable()),
    )
    monkeypatch.setattr(
        invisible,
        "_detect_trustmark",
        lambda *_: (_ for _ in ()).throw(DecoderUnavailable()),
    )

    result = run_invisible_watermarks(_make_analysis_context(_make_test_image_bytes()))

    assert result["status"] == "NOT AVAILABLE"
    assert result["data"]["attempted_decoders"] == []
    assert set(result["data"]["unavailable_decoders"]) == {"open_dwt_dct", "adobe_trustmark"}


def test_invisible_analyzer_not_found_after_decoder_runs(monkeypatch):
    from veracity.analyzers import invisible

    _enable_invisible_decoders("open_dwt_dct")
    monkeypatch.setattr(invisible, "_detect_open_dwt_dct_watermark", lambda *_: None)
    monkeypatch.setattr(
        invisible,
        "_detect_trustmark",
        lambda *_: (_ for _ in ()).throw(DecoderUnavailable()),
    )

    result = run_invisible_watermarks(_make_analysis_context(_make_test_image_bytes()))

    assert result["status"] == "NOT FOUND"
    assert result["data"]["findings"] == []
    assert result["data"]["attempted_decoders"] == ["open_dwt_dct"]
    assert "inconclusive" in result["summary"].lower()


def test_invisible_analyzer_reports_open_watermark_hit(monkeypatch):
    from veracity.analyzers import invisible

    _enable_invisible_decoders("open_dwt_dct")
    monkeypatch.setattr(
        invisible,
        "_detect_open_dwt_dct_watermark",
        lambda *_: {
            "label": "Open DWT-DCT watermark",
            "scheme": "Stable Diffusion XL",
        },
    )
    monkeypatch.setattr(
        invisible,
        "_detect_trustmark",
        lambda *_: (_ for _ in ()).throw(DecoderUnavailable()),
    )

    result = run_invisible_watermarks(_make_analysis_context(_make_test_image_bytes()))

    assert result["status"] == "FOUND"
    assert result["data"]["findings"][0]["scheme"] == "Stable Diffusion XL"


def test_invisible_analyzer_reports_trustmark_hit(monkeypatch):
    from veracity.analyzers import invisible

    _enable_invisible_decoders("adobe_trustmark")
    monkeypatch.setattr(
        invisible,
        "_detect_open_dwt_dct_watermark",
        lambda *_: (_ for _ in ()).throw(DecoderUnavailable()),
    )
    monkeypatch.setattr(
        invisible,
        "_detect_trustmark",
        lambda *_: {
            "label": "Adobe TrustMark",
            "scheme": "Adobe TrustMark variant P, schema 0",
        },
    )

    result = run_invisible_watermarks(_make_analysis_context(_make_test_image_bytes()))

    assert result["status"] == "FOUND"
    assert result["data"]["findings"][0]["label"] == "Adobe TrustMark"


def test_invisible_analyzer_decoder_failure_is_nonfatal(monkeypatch):
    from veracity.analyzers import invisible

    _enable_invisible_decoders("open_dwt_dct")
    monkeypatch.setattr(
        invisible,
        "_detect_open_dwt_dct_watermark",
        lambda *_: (_ for _ in ()).throw(RuntimeError("decoder exploded")),
    )
    monkeypatch.setattr(
        invisible,
        "_detect_trustmark",
        lambda *_: (_ for _ in ()).throw(DecoderUnavailable()),
    )

    result = run_invisible_watermarks(_make_analysis_context(_make_test_image_bytes()))

    assert result["status"] == "NOT AVAILABLE"
    assert "decoder exploded" in result["data"]["errors"][0]


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
    assert "visually similar image" in result["summary"]  # singular for 1 match
    matches = result["data"]["matches"]
    assert len(matches) == 1
    assert result["data"]["has_distant_matches"] is True
    assert matches[0]["fact_data"] == "Signed by Example Corp"


def test_run_c2pa_ignores_self_neighbor_fact(monkeypatch):
    context = AnalysisContext(
        image_bytes=b"payload",
        phash="0011ffaa0011ffaa",
        whash="0011ffaa0011ffbb",
        registry_id=42,
        neighbors=[
            SimpleNamespace(
                id=42,
                phash="0011ffaa0011ffaa",
                whash="0011ffaa0011ffbb",
                facts=[SimpleNamespace(analyzer="c2pa", data="Signed by Self")],
                sources=[SimpleNamespace(url="https://example.com/self")],
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

    assert result["status"] == "NOT FOUND"
    assert result["data"]["has_distant_matches"] is False
    assert result["data"]["matches"] == []


def test_build_vote_breakdown_handles_zero_totals():
    breakdown = _build_vote_breakdown(real=0, edited=0, ai=0)
    assert breakdown["total"] == 0
    for segment in breakdown["segments"]:
        assert segment["count"] == 0
        assert segment["percent"] == 0


def test_identify_producer_prefers_specific_sources():
    from veracity.analyzers.c2pa import identify_producer

    # Software on the action wins over the certificate issuer.
    assert identify_producer(
        software_agents=["Claude 5"], claim_generators=[], issuer="Anthropic, PBC"
    ) == "Claude (Anthropic)"
    # A Pixel camera signed by Google isn't mistaken for Gemini.
    assert identify_producer(
        software_agents=[], claim_generators=["Google Pixel Camera 10"], issuer="Google LLC"
    ) == "Google Pixel camera"
    assert identify_producer(
        software_agents=[], claim_generators=["ChatGPT"], issuer="OpenAI"
    ) == "ChatGPT (OpenAI)"
    assert identify_producer(software_agents=[], claim_generators=["GIMP"], issuer="") == ""


def test_generator_vocabulary_scopes_ambiguous_names_to_software_fields():
    from veracity.analyzers.exif import _detect_ai_metadata

    def tools(chunks):
        return [f["tool"] for f in _detect_ai_metadata(chunks, b"")]

    # Unambiguous product names count in free-text generator fields.
    assert tools({"ImageDescription": "Made with Recraft v3"})
    assert tools({"Title": "Sora render"}) == []
    # Ambiguous names only count where the field names the software.
    assert tools({"Title": "Firefly season at the lake"}) == []
    assert tools({"Artist": "Leonardo da Vinci"}) == []
    assert tools({"Software": "Adobe Firefly"})
    assert tools({"Software": "Sora"})
    # PNG "Creator" often holds a person's name, so only unambiguous names count.
    assert tools({"Creator": "Luma Chen"}) == []
    assert tools({"Creator": "Midjourney"})
    # Versioned names and hyphenated spellings.
    assert tools({"ImageDescription": "Generated with FLUX.1 [dev]"})
    assert tools({"Software": "nano-banana"})
    # Whole words only: "Fluxus" isn't FLUX.
    assert tools({"Software": "Fluxus Editor"}) == []



def test_identify_producer_matches_whole_words_only():
    from veracity.analyzers.c2pa import identify_producer

    def label(agent="", generator="", issuer=""):
        return identify_producer(
            software_agents=[agent] if agent else [],
            claim_generators=[generator] if generator else [],
            issuer=issuer,
        )

    assert label("Pixelmator Pro") == ""
    assert label("Affinity Designer 2") == ""
    assert label("Soraya Studio") == ""
    assert label(generator="CanonicalCorp") == ""
    assert label(generator="Microsoft Designer") == "Microsoft"
    # The first action (usually "created") wins over later edits.
    assert identify_producer(
        software_agents=["Google Pixel Camera", "Gemini"], claim_generators=[], issuer=""
    ) == "Google Pixel camera"
    # The issuer can name a vendor but never implies a device.
    assert label(issuer="Sony Corporation") == ""
    assert label(issuer="OpenAI") == "OpenAI"


def test_c2pa_mime_detection_handles_mpo_and_tiff():
    import io
    from PIL import Image
    from veracity.analyzers.c2pa import _detect_mime_type

    def encode(fmt, **kwargs):
        buf = io.BytesIO()
        Image.new("RGB", (32, 32)).save(buf, format=fmt, **kwargs)
        return buf.getvalue()

    frames = [Image.new("RGB", (32, 32)), Image.new("RGB", (32, 32), (9, 9, 9))]
    mpo = io.BytesIO()
    frames[0].save(mpo, format="MPO", save_all=True, append_images=frames[1:])
    assert _detect_mime_type(mpo.getvalue()) == "image/jpeg"
    assert _detect_mime_type(encode("TIFF")) == "image/tiff"

import io
import json
import re

import imagehash
from PIL import Image

from conftest import _make_test_image_bytes
from veracity.registry import (
    ConsensusSnapshot,
    FactSnapshot,
    NeighborSnapshot,
    SourceSnapshot,
    SynthIDSnapshot,
)
from veracity.analyzers.context import AnalysisContext
from veracity.analyzers.synthid import run_synthid


def _make_context(
    neighbors=None,
    phash="abcdef1234567890",
    whash="1234567890abcdef",
    registry_id=1,
):
    return AnalysisContext(
        image_bytes=b"fake",
        phash=phash,
        whash=whash,
        registry_id=registry_id,
        neighbors=neighbors or [],
    )


def _make_neighbor(
    id=1,
    phash="abcdef1234567890",
    whash="1234567890abcdef",
    synthid=None,
    consensus=None,
    sources=(),
    facts=(),
    created_at=None,
):
    return NeighborSnapshot(
        id=id,
        phash=phash,
        whash=whash,
        created_at=created_at,
        consensus=consensus,
        sources=sources,
        facts=facts,
        synthid=synthid,
    )


# --- Display state tests ---


def test_manual_state_no_reports():
    """No reports at all -> MANUAL status."""
    context = _make_context(neighbors=[
        _make_neighbor(id=1, synthid=None),
    ])
    result = run_synthid(context)
    assert result["status"] == "MANUAL"
    assert result["data"]["display_state"] == "manual"
    assert result["data"]["score"] == 0


def test_manual_state_empty_synthid():
    """SynthID snapshot with zero counts -> MANUAL."""
    context = _make_context(neighbors=[
        _make_neighbor(id=1, synthid=SynthIDSnapshot(detected=0, not_detected=0)),
    ])
    result = run_synthid(context)
    assert result["status"] == "MANUAL"
    assert result["data"]["display_state"] == "manual"


def test_checked_state_only_negative():
    """Only not_detected reports on same entry -> CHECKED."""
    context = _make_context(neighbors=[
        _make_neighbor(id=1, synthid=SynthIDSnapshot(detected=0, not_detected=3)),
    ])
    result = run_synthid(context)
    assert result["status"] == "CHECKED"
    assert result["data"]["display_state"] == "checked"
    assert "3 users" in result["summary"]
    assert result["data"]["score"] == 0


def test_reported_state_low_confidence():
    """1-3 weighted positive reports -> REPORTED with caveat."""
    context = _make_context(neighbors=[
        _make_neighbor(id=1, synthid=SynthIDSnapshot(detected=2, not_detected=0)),
    ])
    result = run_synthid(context)
    assert result["status"] == "REPORTED"
    assert result["data"]["display_state"] == "reported"
    assert result["data"]["caveat"] is not None
    assert result["data"]["score"] == 2.0


def test_detected_state_high_confidence():
    """4+ weighted positive reports -> DETECTED."""
    context = _make_context(neighbors=[
        _make_neighbor(id=1, synthid=SynthIDSnapshot(detected=5, not_detected=0)),
    ])
    result = run_synthid(context)
    assert result["status"] == "DETECTED"
    assert result["data"]["display_state"] == "detected"
    assert result["data"]["caveat"] is None
    assert result["data"]["score"] == 5.0


# --- Tier weighting tests ---


def test_tier_a_weight():
    """Same entry (tier A) gets weight 1.0."""
    context = _make_context(registry_id=1, neighbors=[
        _make_neighbor(id=1, synthid=SynthIDSnapshot(detected=3, not_detected=0)),
    ])
    result = run_synthid(context)
    assert result["data"]["score"] == 3.0  # 3 * 1.0


def test_tier_b_weight():
    """Same phash, different id (tier B) gets weight 0.75."""
    context = _make_context(
        registry_id=1,
        phash="abcdef1234567890",
        whash="1234567890abcdef",
        neighbors=[
            _make_neighbor(
                id=2,
                phash="abcdef1234567890",
                whash="1234567890abcdef",
                synthid=SynthIDSnapshot(detected=4, not_detected=0),
            ),
        ],
    )
    result = run_synthid(context)
    assert result["data"]["score"] == 3.0  # 4 * 0.75


def test_tier_c_weight():
    """Similar hash, different id (tier C) gets weight 0.5."""
    # Use a hash that differs by a small hamming distance (not 0)
    # We need phash distance > 0 and whash distance > 0, but within threshold
    context = _make_context(
        registry_id=1,
        phash="abcdef1234567890",
        whash="1234567890abcdef",
        neighbors=[
            _make_neighbor(
                id=3,
                phash="abcdef1234567891",  # differs by 1 bit in last hex char
                whash="1234567890abcdee",  # differs slightly
                synthid=SynthIDSnapshot(detected=4, not_detected=0),
            ),
        ],
    )
    result = run_synthid(context)
    assert result["data"]["score"] == 2.0  # 4 * 0.5


# --- Tier A contradiction ---


def test_tier_a_contradiction_zeroes_contribution():
    """When not_detected >= 3 * detected on same entry, contribution is zeroed."""
    context = _make_context(registry_id=1, neighbors=[
        _make_neighbor(id=1, synthid=SynthIDSnapshot(detected=1, not_detected=3)),
    ])
    result = run_synthid(context)
    assert result["data"]["score"] == 0
    # Should be CHECKED (score 0 with reports present)
    assert result["status"] == "CHECKED"


def test_tier_a_no_contradiction_below_ratio():
    """When not_detected < 3 * detected, contribution is NOT zeroed."""
    context = _make_context(registry_id=1, neighbors=[
        _make_neighbor(id=1, synthid=SynthIDSnapshot(detected=2, not_detected=5)),
    ])
    result = run_synthid(context)
    # not_detected (5) < 3 * detected (6), so contribution = 2
    assert result["data"]["score"] == 2.0


# --- Contested flag ---


def test_contested_flag_mixed_reports():
    """Mixed reports on same entry within 1:1 to 3:1 ratio -> contested."""
    context = _make_context(registry_id=1, neighbors=[
        _make_neighbor(id=1, synthid=SynthIDSnapshot(detected=2, not_detected=4)),
    ])
    result = run_synthid(context)
    assert result["data"]["contested"] is True


def test_contested_flag_not_set_when_no_positives():
    """No detected reports -> contested is False."""
    context = _make_context(registry_id=1, neighbors=[
        _make_neighbor(id=1, synthid=SynthIDSnapshot(detected=0, not_detected=5)),
    ])
    result = run_synthid(context)
    assert result["data"]["contested"] is False


def test_contested_flag_not_set_beyond_ratio():
    """not_detected > 3 * detected -> NOT contested (it's a contradiction instead)."""
    context = _make_context(registry_id=1, neighbors=[
        _make_neighbor(id=1, synthid=SynthIDSnapshot(detected=1, not_detected=4)),
    ])
    result = run_synthid(context)
    # ratio = 4/1 = 4.0, which is > 3, so contested is False
    assert result["data"]["contested"] is False


# --- Similar image propagation ---


def test_similar_image_propagation():
    """Positive on neighbor shows as similar match."""
    context = _make_context(
        registry_id=1,
        phash="abcdef1234567890",
        whash="1234567890abcdef",
        neighbors=[
            # Same entry, no reports
            _make_neighbor(id=1, synthid=None),
            # Similar image with positive reports
            _make_neighbor(
                id=2,
                phash="abcdef1234567890",
                whash="1234567890abcdef",
                synthid=SynthIDSnapshot(detected=2, not_detected=0),
            ),
        ],
    )
    result = run_synthid(context)
    assert len(result["data"]["similar_images"]) == 1
    assert result["data"]["similar_images"][0]["detected"] == 2
    # Only from similar -> summary should mention "similar image"
    assert result["data"]["this_image"]["detected"] == 0
    assert "similar image" in result["summary"]


# --- Vote endpoint tests ---


def _upload_and_get_ids(client):
    """Upload an image and extract analysis_id and phash."""
    image_bytes = _make_test_image_bytes()
    data = {
        "file": (io.BytesIO(image_bytes), "test.png"),
        "image_url": "",
    }
    resp = client.post("/analyze", data=data, content_type="multipart/form-data")
    assert resp.status_code == 200

    body = resp.data.decode("utf-8")
    match = re.search(r"/analysis/([a-f0-9]+)/analyzers/", body)
    assert match is not None
    analysis_id = match.group(1)

    with Image.open(io.BytesIO(image_bytes)) as img:
        target_hash = imagehash.phash(img)
    phash = str(target_hash)

    return analysis_id, phash


def test_synthid_report_creates_record(client, app):
    """POST /synthid-report creates SynthIDReport."""
    analysis_id, phash = _upload_and_get_ids(client)

    data = {"phash": phash, "report": "detected", "analysis_id": analysis_id}
    resp = client.post("/synthid-report", data=data, follow_redirects=False)
    assert resp.status_code == 302

    from veracity.models import SynthIDReport

    with app.app_context():
        reports = SynthIDReport.query.all()
        assert len(reports) == 1
        assert reports[0].result == "detected"


def test_synthid_report_change(client, app):
    """User changes from detected to not_detected."""
    analysis_id, phash = _upload_and_get_ids(client)

    data = {"phash": phash, "report": "detected", "analysis_id": analysis_id}
    client.post("/synthid-report", data=data)

    data["report"] = "not_detected"
    client.post("/synthid-report", data=data)

    from veracity.models import SynthIDReport

    with app.app_context():
        reports = SynthIDReport.query.all()
        assert len(reports) == 1
        assert reports[0].result == "not_detected"


def test_synthid_report_invalid_choice(client):
    """Invalid report choice redirects with error."""
    analysis_id, phash = _upload_and_get_ids(client)
    data = {"phash": phash, "report": "invalid", "analysis_id": analysis_id}
    resp = client.post("/synthid-report", data=data, follow_redirects=False)
    assert resp.status_code == 302


def test_htmx_synthid_report_returns_fragment(client, app):
    """HTMX vote returns refreshed fragment with HX-Trigger."""
    analysis_id, phash = _upload_and_get_ids(client)

    data = {"phash": phash, "report": "detected", "analysis_id": analysis_id}
    resp = client.post(
        "/synthid-report",
        data=data,
        headers={"HX-Request": "true"},
    )

    assert resp.status_code == 200
    assert "HX-Trigger" in resp.headers
    trigger = json.loads(resp.headers["HX-Trigger"])
    assert "showToast" in trigger
    assert "recorded" in trigger["showToast"].lower()
    # Should contain the synthid analyzer fragment
    assert b"synthid" in resp.data.lower()


def test_htmx_synthid_report_unchanged(client, app):
    """Submitting same report again returns 'unchanged' toast."""
    analysis_id, phash = _upload_and_get_ids(client)

    data = {"phash": phash, "report": "detected", "analysis_id": analysis_id}
    client.post(
        "/synthid-report",
        data=data,
        headers={"HX-Request": "true"},
    )

    # Same report again
    resp = client.post(
        "/synthid-report",
        data=data,
        headers={"HX-Request": "true"},
    )

    assert resp.status_code == 200
    trigger = json.loads(resp.headers["HX-Trigger"])
    assert "already" in trigger["showToast"].lower()

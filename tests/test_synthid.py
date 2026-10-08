import io
import json
import re

import imagehash
from PIL import Image

from conftest import _make_test_image_bytes
from veracity.registry import NeighborSnapshot, SynthIDSnapshot
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


def _snapshot(google=0, openai=0, meta=0, negative=0):
    return SynthIDSnapshot(
        google_positive=google,
        openai_positive=openai,
        meta_positive=meta,
        negative=negative,
    )


def _make_neighbor(
    id=1,
    phash="abcdef1234567890",
    whash="1234567890abcdef",
    synthid=None,
):
    return NeighborSnapshot(
        id=id,
        phash=phash,
        whash=whash,
        created_at=None,
        consensus=None,
        sources=(),
        facts=(),
        synthid=synthid,
    )


def test_manual_state_no_reports():
    context = _make_context(neighbors=[_make_neighbor(id=1, synthid=None)])

    result = run_synthid(context)

    assert result["status"] == "MANUAL"
    assert result["data"]["display_state"] == "manual"
    assert result["data"]["score"] == 0


def test_manual_state_empty_portal_snapshot():
    context = _make_context(neighbors=[_make_neighbor(id=1, synthid=_snapshot())])

    result = run_synthid(context)

    assert result["status"] == "MANUAL"
    assert result["data"]["display_state"] == "manual"


def test_checked_state_only_negative():
    context = _make_context(neighbors=[_make_neighbor(id=1, synthid=_snapshot(negative=3))])

    result = run_synthid(context)

    assert result["status"] == "CHECKED"
    assert result["data"]["display_state"] == "checked"
    assert result["data"]["totals"]["negative"] == 3
    assert "3 users" in result["summary"]


def test_reported_state_low_confidence_google_positive():
    context = _make_context(neighbors=[_make_neighbor(id=1, synthid=_snapshot(google=2))])

    result = run_synthid(context)

    assert result["status"] == "REPORTED"
    assert result["data"]["display_state"] == "reported"
    assert result["data"]["scores"]["google_positive"] == 2.0
    assert result["data"]["caveat"] is not None


def test_detected_state_high_confidence_openai_positive():
    context = _make_context(neighbors=[_make_neighbor(id=1, synthid=_snapshot(openai=5))])

    result = run_synthid(context)

    assert result["status"] == "DETECTED"
    assert result["data"]["display_state"] == "detected"
    assert result["data"]["scores"]["openai_positive"] == 5.0
    assert result["data"]["caveat"] is None


def test_contested_when_both_provider_positive_buckets_exist():
    context = _make_context(neighbors=[_make_neighbor(id=1, synthid=_snapshot(google=1, openai=1))])

    result = run_synthid(context)

    assert result["status"] == "REPORTED"
    assert result["data"]["display_state"] == "contested"
    assert result["data"]["contested"] is True
    assert result["data"]["verification_portals"]["verdict"] == "contested"


def test_meta_positive_is_detected_and_contests_other_provider():
    detected = run_synthid(
        _make_context(neighbors=[_make_neighbor(id=1, synthid=_snapshot(meta=5))])
    )
    contested = run_synthid(
        _make_context(
            neighbors=[_make_neighbor(id=1, synthid=_snapshot(openai=1, meta=1))]
        )
    )

    assert detected["status"] == "DETECTED"
    assert detected["data"]["scores"]["meta_positive"] == 5.0
    assert contested["data"]["display_state"] == "contested"


def test_checker_rows_preserve_portal_counts():
    context = _make_context(neighbors=[_make_neighbor(id=1, synthid=_snapshot(google=2, negative=1))])

    result = run_synthid(context)

    rows = {row["result"]: row for row in result["data"]["checker_rows"]}
    assert result["data"]["totals"] == {
        "google_positive": 2,
        "openai_positive": 0,
        "meta_positive": 0,
        "negative": 1,
    }
    assert rows["google_positive"]["count"] == 2
    assert rows["openai_positive"]["count"] == 0
    assert rows["meta_positive"]["count"] == 0
    assert rows["negative"]["count"] == 1


def test_tier_a_weight():
    context = _make_context(registry_id=1, neighbors=[_make_neighbor(id=1, synthid=_snapshot(google=3))])

    result = run_synthid(context)

    assert result["data"]["scores"]["google_positive"] == 3.0


def test_tier_b_weight():
    context = _make_context(
        registry_id=1,
        phash="abcdef1234567890",
        whash="1234567890abcdef",
        neighbors=[
            _make_neighbor(
                id=2,
                phash="abcdef1234567890",
                whash="1234567890abcdef",
                synthid=_snapshot(google=4),
            ),
        ],
    )

    result = run_synthid(context)

    assert result["data"]["scores"]["google_positive"] == 3.0


def test_tier_c_weight():
    context = _make_context(
        registry_id=1,
        phash="abcdef1234567890",
        whash="1234567890abcdef",
        neighbors=[
            _make_neighbor(
                id=3,
                phash="abcdef1234567891",
                whash="1234567890abcdee",
                synthid=_snapshot(google=4),
            ),
        ],
    )

    result = run_synthid(context)

    assert result["data"]["scores"]["google_positive"] == 2.0


def test_tier_a_negative_contradiction_zeroes_positive_contribution():
    context = _make_context(registry_id=1, neighbors=[_make_neighbor(id=1, synthid=_snapshot(google=1, negative=3))])

    result = run_synthid(context)

    assert result["data"]["score"] == 0
    assert result["status"] == "CHECKED"


def test_similar_image_propagation():
    context = _make_context(
        registry_id=1,
        phash="abcdef1234567890",
        whash="1234567890abcdef",
        neighbors=[
            _make_neighbor(id=1, synthid=None),
            _make_neighbor(
                id=2,
                phash="abcdef1234567890",
                whash="1234567890abcdef",
                synthid=_snapshot(google=2),
            ),
        ],
    )

    result = run_synthid(context)

    assert len(result["data"]["similar_images"]) == 1
    assert result["data"]["similar_images"][0]["google_positive"] == 2
    assert result["data"]["this_image"]["google_positive"] == 0
    assert "similar image" in result["summary"]


def _upload_and_get_ids(client):
    image_bytes = _make_test_image_bytes()
    data = {
        "file": (io.BytesIO(image_bytes), "test.png"),
        "image_url": "",
    }
    resp = client.post("/analyze", data=data, content_type="multipart/form-data", follow_redirects=True)
    assert resp.status_code == 200

    body = resp.data.decode("utf-8")
    match = re.search(r"/analysis/([a-f0-9]+)/analyzers/", body)
    assert match is not None
    analysis_id = match.group(1)

    with Image.open(io.BytesIO(image_bytes)) as img:
        target_hash = imagehash.phash(img)
    return analysis_id, str(target_hash)


def test_synthid_report_creates_record(client, app):
    analysis_id, phash = _upload_and_get_ids(client)

    resp = client.post(
        "/synthid-report",
        data={"phash": phash, "report": "google_positive", "analysis_id": analysis_id},
        follow_redirects=False,
    )

    from veracity.models import SynthIDReport

    assert resp.status_code == 302
    with app.app_context():
        reports = SynthIDReport.query.all()
        assert len(reports) == 1
        assert reports[0].result == "google_positive"


def test_synthid_report_change_updates_single_row(client, app):
    analysis_id, phash = _upload_and_get_ids(client)

    data = {"phash": phash, "report": "google_positive", "analysis_id": analysis_id}
    client.post("/synthid-report", data=data)
    data["report"] = "negative"
    client.post("/synthid-report", data=data)

    from veracity.models import SynthIDReport

    with app.app_context():
        reports = SynthIDReport.query.all()
        assert len(reports) == 1
        assert reports[0].result == "negative"


def test_synthid_report_accepts_meta_positive(client, app):
    analysis_id, phash = _upload_and_get_ids(client)

    client.post(
        "/synthid-report",
        data={
            "phash": phash,
            "report": "meta_positive",
            "analysis_id": analysis_id,
        },
    )

    from veracity.models import SynthIDReport

    with app.app_context():
        report = SynthIDReport.query.one()
        assert report.result == "meta_positive"


def test_synthid_report_accepts_legacy_post_values(client, app):
    analysis_id, phash = _upload_and_get_ids(client)

    client.post(
        "/synthid-report",
        data={
            "phash": phash,
            "report": "detected",
            "analysis_id": analysis_id,
            "provider": "openai",
            "detector": "openai_verify",
        },
    )
    client.post(
        "/synthid-report",
        data={
            "phash": phash,
            "report": "not_detected",
            "analysis_id": analysis_id,
        },
    )

    from veracity.models import SynthIDReport

    with app.app_context():
        reports = SynthIDReport.query.all()
        assert len(reports) == 1
        assert reports[0].result == "negative"


def test_synthid_report_invalid_choice(client, app):
    analysis_id, phash = _upload_and_get_ids(client)

    resp = client.post(
        "/synthid-report",
        data={"phash": phash, "report": "invalid", "analysis_id": analysis_id},
        follow_redirects=False,
    )

    from veracity.models import SynthIDReport

    assert resp.status_code == 302
    with app.app_context():
        assert SynthIDReport.query.count() == 0


def test_htmx_synthid_report_returns_fragment(client):
    analysis_id, phash = _upload_and_get_ids(client)

    resp = client.post(
        "/synthid-report",
        data={"phash": phash, "report": "openai_positive", "analysis_id": analysis_id},
        headers={"HX-Request": "true"},
    )

    assert resp.status_code == 200
    assert "HX-Trigger" in resp.headers
    trigger = json.loads(resp.headers["HX-Trigger"])
    assert "recorded" in trigger["showToast"].lower()
    assert b"synthid" in resp.data.lower()


def test_htmx_synthid_report_unchanged(client):
    analysis_id, phash = _upload_and_get_ids(client)
    data = {"phash": phash, "report": "openai_positive", "analysis_id": analysis_id}

    client.post("/synthid-report", data=data, headers={"HX-Request": "true"})
    resp = client.post("/synthid-report", data=data, headers={"HX-Request": "true"})

    assert resp.status_code == 200
    trigger = json.loads(resp.headers["HX-Trigger"])
    assert "already" in trigger["showToast"].lower()


def test_synthid_mini_fragment_includes_four_portal_forms(client):
    analysis_id, _ = _upload_and_get_ids(client)

    fragment = client.get(f"/analysis/{analysis_id}/analyzers/synthid?mini=1")

    assert fragment.status_code == 200
    assert fragment.data.count(b'name="mini" value="1"') == 4
    assert b'name="report" value="google_positive"' in fragment.data
    assert b'name="report" value="openai_positive"' in fragment.data
    assert b'name="report" value="meta_positive"' in fragment.data
    assert b'name="report" value="negative"' in fragment.data


def test_synthid_fragment_includes_checker_actions(client):
    analysis_id, _ = _upload_and_get_ids(client)

    fragment = client.get(f"/analysis/{analysis_id}/analyzers/synthid")

    assert fragment.status_code == 200
    html = fragment.data.decode()
    assert "https://synthid.com/" in html
    assert "Gemini" not in html
    assert 'aria-label="Report Google SynthID Detector: Says Google"' in html
    assert 'aria-label="Report Google SynthID Detector: Says OpenAI"' in html
    assert 'aria-label="Report Meta Identify: Positive"' in html
    assert 'aria-label="Report Negative"' in html
    assert 'aria-label="Open Meta Identify"' in html
    # SynthID Detector comes first; OpenAI Verify is a fallback behind a disclosure.
    assert html.index("Google SynthID Detector") < html.index("Meta Identify")
    assert html.index("Can't use the SynthID Detector?") < html.index("OpenAI Verify")
    assert 'aria-label="Report OpenAI Verify: Positive"' in html


def test_htmx_synthid_report_mini_returns_mini_fragment(client):
    analysis_id, phash = _upload_and_get_ids(client)

    resp = client.post(
        "/synthid-report",
        data={
            "phash": phash,
            "report": "google_positive",
            "analysis_id": analysis_id,
            "mini": "1",
        },
        headers={"HX-Request": "true"},
    )

    assert resp.status_code == 200
    assert b"mini-card-content" in resp.data
    assert b'id="analyzer-row-synthid"' not in resp.data

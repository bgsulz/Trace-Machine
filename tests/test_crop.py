import io
import json
import re

import pytest

from conftest import _make_entropy_image_bytes


def _extract_analysis_id(response_text: str) -> str:
    match = re.search(r"/analysis/([a-f0-9]+)/analyzers", response_text)
    assert match, "analysis_id not found in response body"
    return match.group(1)


def _upload_entropy_image(client):
    image_bytes = _make_entropy_image_bytes()
    data = {"file": (io.BytesIO(image_bytes), "entropy.png"), "image_url": ""}
    resp = client.post("/analyze", data=data, content_type="multipart/form-data")
    assert resp.status_code == 200
    return resp, image_bytes


def test_crop_endpoint_creates_containment(client, app):
    initial, _ = _upload_entropy_image(client)
    analysis_id = _extract_analysis_id(initial.data.decode("utf-8"))

    crop_data = {
        "crop_left": "0.0",
        "crop_top": "0.0",
        "crop_width": "0.75",
        "crop_height": "0.75",
    }
    resp = client.post(f"/analysis/{analysis_id}/crop", data=crop_data)
    assert resp.status_code == 200
    assert b"Provenance Report" in resp.data

    from veracity.models import ImageContainment

    with app.app_context():
        links = ImageContainment.query.all()
        assert len(links) == 1
        crop_box = json.loads(links[0].crop_box_json)
        assert pytest.approx(crop_box[2], rel=1e-2) == 0.75
        assert pytest.approx(crop_box[3], rel=1e-2) == 0.75


def test_crop_rejects_tiny_selection(client, app):
    initial, _ = _upload_entropy_image(client)
    analysis_id = _extract_analysis_id(initial.data.decode("utf-8"))

    crop_data = {
        "crop_left": "0.0",
        "crop_top": "0.0",
        "crop_width": "0.05",
        "crop_height": "0.05",
    }
    resp = client.post(f"/analysis/{analysis_id}/crop", data=crop_data)
    assert resp.status_code == 200
    assert b"Provenance Report" in resp.data

    from veracity.models import ImageContainment

    with app.app_context():
        assert ImageContainment.query.count() == 0


def test_containment_section_shows_for_known_child(client, app):
    initial, image_bytes = _upload_entropy_image(client)
    analysis_id = _extract_analysis_id(initial.data.decode("utf-8"))

    crop_data = {
        "crop_left": "0.0",
        "crop_top": "0.0",
        "crop_width": "0.75",
        "crop_height": "0.75",
    }
    resp = client.post(f"/analysis/{analysis_id}/crop", data=crop_data)
    assert resp.status_code == 200

    from veracity import db
    from veracity.models import ImageConsensus, ImageContainment

    with app.app_context():
        link = ImageContainment.query.first()
        assert link is not None
        child_id = link.child_id
        consensus = ImageConsensus.query.filter_by(image_id=child_id).first()
        if consensus is None:
            consensus = ImageConsensus(image_id=child_id)
            db.session.add(consensus)
        consensus.vote_real = 1
        consensus.vote_ai = 0
        consensus.vote_edited = 0
        db.session.commit()

    data = {"file": (io.BytesIO(image_bytes), "entropy.png"), "image_url": ""}
    resp_second = client.post("/analyze", data=data, content_type="multipart/form-data")
    assert resp_second.status_code == 200
    body = resp_second.data.decode("utf-8")
    assert "This image contains regions that match previously analyzed images." in body
    assert "contained-regions-table" in body

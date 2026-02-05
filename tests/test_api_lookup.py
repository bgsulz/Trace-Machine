import json

from veracity import db
from veracity.models import (
    ImageConsensus,
    ImageRegistry,
    ImageSource,
    ProvenanceFact,
    SynthIDReport,
)


def _seed_image(app, phash="aabbccdd11223344", url="https://example.com/photo.jpg",
                vote_real=0, vote_edited=0, vote_ai=0,
                facts=(), synthid_reports=()):
    """Insert a registry entry with optional consensus, facts, and synthid reports."""
    with app.app_context():
        reg = ImageRegistry(phash=phash, whash=phash)
        db.session.add(reg)
        db.session.flush()

        src = ImageSource(image_id=reg.id, url=url)
        db.session.add(src)

        if vote_real or vote_edited or vote_ai:
            cons = ImageConsensus(
                image_id=reg.id,
                vote_real=vote_real,
                vote_edited=vote_edited,
                vote_ai=vote_ai,
            )
            db.session.add(cons)

        for analyzer, data in facts:
            db.session.add(ProvenanceFact(image_id=reg.id, analyzer=analyzer, data=data))

        for i, result in enumerate(synthid_reports):
            db.session.add(SynthIDReport(
                image_id=reg.id,
                voter_id=f"voter-{i}",
                result=result,
            ))

        db.session.commit()
        return reg.id


# --- Tests ---


def test_empty_results(client):
    """Querying with unknown URLs returns empty results dict."""
    resp = client.post(
        "/api/lookup",
        json={"urls": ["https://nowhere.example.com/nope.jpg"]},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["results"] == {}


def test_known_url_match(client, app):
    """A URL that exists in ImageSource is returned with provenance data."""
    url = "https://example.com/known.jpg"
    _seed_image(app, url=url, vote_real=10, vote_edited=2, vote_ai=1)

    resp = client.post("/api/lookup", json={"urls": [url]})
    assert resp.status_code == 200
    data = resp.get_json()
    assert url in data["results"]
    entry = data["results"][url]
    assert entry["vote_real"] == 10
    assert entry["vote_edited"] == 2
    assert entry["vote_ai"] == 1
    assert entry["total_votes"] == 13
    assert entry["verdict"] == "real"


def test_dethumbnail_expansion(client, app):
    """A Reddit preview URL should match via dethumbnail expansion."""
    # The stored URL is the dethumbnailed i.redd.it form
    stored_url = "https://i.redd.it/abc1234567890.jpg"
    _seed_image(app, url=stored_url, phash="1111111111111111", vote_ai=5)

    # The input URL is the preview form that dethumbnails to the stored URL
    preview_url = "https://preview.redd.it/abc1234567890.jpg?width=640"
    resp = client.post("/api/lookup", json={"urls": [preview_url]})
    assert resp.status_code == 200
    data = resp.get_json()
    assert preview_url in data["results"]
    assert data["results"][preview_url]["vote_ai"] == 5


def test_max_url_cap(client):
    """More than 50 URLs returns 400."""
    urls = [f"https://example.com/{i}.jpg" for i in range(51)]
    resp = client.post("/api/lookup", json={"urls": urls})
    assert resp.status_code == 400
    assert "50" in resp.get_json()["error"]


def test_exactly_max_urls_accepted(client):
    """Exactly 50 URLs is accepted."""
    urls = [f"https://example.com/{i}.jpg" for i in range(50)]
    resp = client.post("/api/lookup", json={"urls": urls})
    assert resp.status_code == 200


def test_invalid_json_body(client):
    """Non-JSON body returns 400."""
    resp = client.post(
        "/api/lookup",
        data="not json",
        content_type="text/plain",
    )
    assert resp.status_code == 400


def test_missing_urls_key(client):
    """JSON without 'urls' key returns 400."""
    resp = client.post("/api/lookup", json={"bad": "payload"})
    assert resp.status_code == 400


def test_c2pa_fact_reflected(client, app):
    """When a c2pa ProvenanceFact exists, c2pa is True in the response."""
    url = "https://example.com/c2pa.jpg"
    _seed_image(app, url=url, phash="c2c2c2c2c2c2c2c2", facts=[("c2pa", "signed")])

    resp = client.post("/api/lookup", json={"urls": [url]})
    data = resp.get_json()
    assert data["results"][url]["c2pa"] is True


def test_no_c2pa_fact(client, app):
    """Without c2pa ProvenanceFact, c2pa is False."""
    url = "https://example.com/noc2pa.jpg"
    _seed_image(app, url=url, phash="d3d3d3d3d3d3d3d3")

    resp = client.post("/api/lookup", json={"urls": [url]})
    data = resp.get_json()
    assert data["results"][url]["c2pa"] is False


def test_synthid_detected_reflected(client, app):
    """When more detected than not_detected reports, synthid is True."""
    url = "https://example.com/synthid.jpg"
    _seed_image(
        app, url=url, phash="e4e4e4e4e4e4e4e4",
        synthid_reports=["detected", "detected", "not_detected"],
    )

    resp = client.post("/api/lookup", json={"urls": [url]})
    data = resp.get_json()
    assert data["results"][url]["synthid"] is True


def test_synthid_not_detected_reflected(client, app):
    """When more not_detected than detected reports, synthid is False."""
    url = "https://example.com/nosynthid.jpg"
    _seed_image(
        app, url=url, phash="f5f5f5f5f5f5f5f5",
        synthid_reports=["not_detected", "not_detected", "detected"],
    )

    resp = client.post("/api/lookup", json={"urls": [url]})
    data = resp.get_json()
    assert data["results"][url]["synthid"] is False


def test_no_consensus_verdict_null(client, app):
    """Image with no votes has verdict null and total_votes 0."""
    url = "https://example.com/novotes.jpg"
    _seed_image(app, url=url, phash="0000000000000001")

    resp = client.post("/api/lookup", json={"urls": [url]})
    data = resp.get_json()
    entry = data["results"][url]
    assert entry["verdict"] is None
    assert entry["total_votes"] == 0


def test_non_string_urls_filtered(client, app):
    """Non-string entries in the urls array are silently filtered out."""
    url = "https://example.com/real.jpg"
    _seed_image(app, url=url, phash="abab121234345656", vote_real=1)

    resp = client.post("/api/lookup", json={"urls": [url, 123, None, ""]})
    assert resp.status_code == 200
    data = resp.get_json()
    assert url in data["results"]

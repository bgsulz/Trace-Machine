import io

import imagehash
from PIL import Image

from conftest import _make_test_image_bytes


def test_index_renders_ok(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Analyze an image" in resp.data


def test_analyze_with_file_upload(client, app):
    # CSRF disabled via config
    image_bytes = _make_test_image_bytes()
    data = {
        "file": (io.BytesIO(image_bytes), "test.png"),
        "image_url": "",
    }
    resp = client.post("/analyze", data=data, content_type="multipart/form-data")
    assert resp.status_code == 200
    # result template header
    assert b"Provenance Report" in resp.data
    assert b"Digital Signature (C2PA)" in resp.data
    assert b"Human Consensus" in resp.data


def test_analyze_requires_input(client):
    resp = client.post("/analyze", data={}, content_type="multipart/form-data")
    # should redirect back to index with flash message
    assert resp.status_code == 302
    assert "/" in resp.headers["Location"]


def test_analyze_get_without_url_redirects(client):
    resp = client.get("/analyze")
    assert resp.status_code == 302
    assert "/" in resp.headers["Location"]


def test_analyze_post_with_url_redirects_to_get(client):
    url = "https://example.com/image.png"
    data = {"image_url": url}
    resp = client.post("/analyze", data=data, content_type="multipart/form-data")
    assert resp.status_code == 302
    location = resp.headers.get("Location", "")
    assert "/analyze" in location
    assert f"url={url}" in location


def test_analyze_url_creates_image_source(client, app, monkeypatch):
    import veracity.ingestion as ingestion_module

    dummy_image_bytes = _make_test_image_bytes()

    def fake_fetch(url):
        return dummy_image_bytes, "image/png"

    monkeypatch.setattr(ingestion_module, "fetch_image_bytes", fake_fetch)

    resp = client.get("/analyze?url=https://example.com/image.png")
    assert resp.status_code == 200
    assert b"Provenance Report" in resp.data

    from veracity.models import ImageSource

    with app.app_context():
        rows = ImageSource.query.all()
        assert len(rows) == 1
        row = rows[0]
        assert row.url == "https://example.com/image.png"


def test_analyze_url_creates_image_source_only_once(client, app, monkeypatch):
    import veracity.ingestion as ingestion_module

    dummy_image_bytes = _make_test_image_bytes()

    def fake_fetch(url):
        return dummy_image_bytes, "image/png"

    monkeypatch.setattr(ingestion_module, "fetch_image_bytes", fake_fetch)

    url = "https://example.com/image.png"
    resp1 = client.get(f"/analyze?url={url}")
    assert resp1.status_code == 200
    resp2 = client.get(f"/analyze?url={url}")
    assert resp2.status_code == 200

    from veracity.models import ImageSource

    with app.app_context():
        rows = ImageSource.query.filter_by(url=url).all()
        assert len(rows) == 1


def test_file_upload_does_not_create_image_source(client, app):
    image_bytes = _make_test_image_bytes()
    data = {
        "file": (io.BytesIO(image_bytes), "test.png"),
        "image_url": "",
    }
    resp = client.post("/analyze", data=data, content_type="multipart/form-data")
    assert resp.status_code == 200

    from veracity.models import ImageSource

    with app.app_context():
        assert ImageSource.query.count() == 0


def test_csrf_protection_enabled_by_default(app_csrf):
    client = app_csrf.test_client()

    image_bytes = _make_test_image_bytes()
    data = {
        "file": (io.BytesIO(image_bytes), "test.png"),
        "image_url": "",
    }
    # Missing csrf_token should be rejected with 400 from Flask-WTF
    resp = client.post("/analyze", data=data, content_type="multipart/form-data")
    assert resp.status_code == 400


def test_vote_creates_record_and_increments_counts(client, app):
    # Arrange: run an analysis to generate a Human Consensus hash
    image_bytes = _make_test_image_bytes()
    data = {
        "file": (io.BytesIO(image_bytes), "test.png"),
        "image_url": "",
    }
    resp = client.post("/analyze", data=data, content_type="multipart/form-data")
    assert resp.status_code == 200

    # Compute the same perceptual hash the app uses for this image
    with Image.open(io.BytesIO(image_bytes)) as img:
        target_hash = imagehash.phash(img)
    phash = str(target_hash)

    # Act: submit two votes (one real, one ai)
    vote_data_real = {"phash": phash, "vote": "real"}
    vote_data_edited = {"phash": phash, "vote": "edited"}
    vote_data_ai = {"phash": phash, "vote": "ai"}

    resp_real = client.post("/vote", data=vote_data_real, follow_redirects=True)
    assert resp_real.status_code == 200

    resp_edited = client.post(
        "/vote",
        data=vote_data_edited,
        follow_redirects=True,
        headers={"X-Forwarded-For": "203.0.113.5"},
    )
    assert resp_edited.status_code == 200

    resp_ai = client.post(
        "/vote",
        data=vote_data_ai,
        follow_redirects=True,
        headers={"X-Forwarded-For": "198.51.100.8"},
    )
    assert resp_ai.status_code == 200

    # Assert: database reflects two votes (1 real, 1 ai) and vote history entries
    from veracity.models import ImageConsensus, VoteHistory, ImageRegistry

    with app.app_context():
        registry_row = ImageRegistry.query.filter_by(phash=phash).first()
        assert registry_row is not None

        row = ImageConsensus.query.filter_by(image_id=registry_row.id).first()
        assert row is not None
        assert row.vote_real == 1
        assert row.vote_edited == 1
        assert row.vote_ai == 1

        history_rows = VoteHistory.query.filter_by(image_id=registry_row.id).all()
        assert len(history_rows) == 3


def test_vote_rejects_duplicate_votes(client, app):
    image_bytes = _make_test_image_bytes()
    data = {
        "file": (io.BytesIO(image_bytes), "test.png"),
        "image_url": "",
    }
    resp = client.post("/analyze", data=data, content_type="multipart/form-data")
    assert resp.status_code == 200

    # Compute the same perceptual hash the app uses for this image
    with Image.open(io.BytesIO(image_bytes)) as img:
        target_hash = imagehash.phash(img)
    phash = str(target_hash)

    vote_data = {"phash": phash, "vote": "real"}

    first = client.post("/vote", data=vote_data, follow_redirects=True)
    assert first.status_code == 200

    second = client.post("/vote", data=vote_data, follow_redirects=True)
    assert second.status_code == 200

    from veracity.models import ImageConsensus, VoteHistory, ImageRegistry

    with app.app_context():
        registry_row = ImageRegistry.query.filter_by(phash=phash).first()
        assert registry_row is not None

        row = ImageConsensus.query.filter_by(image_id=registry_row.id).first()
        assert row.vote_real == 1  # not incremented twice
        count = VoteHistory.query.filter_by(image_id=registry_row.id).count()
        assert count == 1


def test_vote_rejects_invalid_payload(client):
    # Missing phash and vote_kind should redirect back to index
    resp = client.post("/vote", data={}, follow_redirects=False)
    assert resp.status_code == 302
    assert "/" in resp.headers.get("Location", "")

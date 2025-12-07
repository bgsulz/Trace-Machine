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


def test_analyze_mini_renders_compact_report(client, monkeypatch):
    import veracity.ingestion as ingestion_module

    dummy_image_bytes = _make_test_image_bytes()

    def fake_fetch(url):
        return dummy_image_bytes, "image/png"

    monkeypatch.setattr(ingestion_module, "fetch_image_bytes", fake_fetch)

    resp = client.get("/analyze-mini?url=https://example.com/mini.png")
    assert resp.status_code == 200
    assert b"Digital Signature (C2PA)" in resp.data


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


def test_analyze_auto_vote_records_and_updates(client, app, monkeypatch):
    import veracity.ingestion as ingestion_module

    dummy_image_bytes = _make_test_image_bytes()

    def fake_fetch(url):
        return dummy_image_bytes, "image/png"

    monkeypatch.setattr(ingestion_module, "fetch_image_bytes", fake_fetch)

    url = "https://example.com/auto.png"

    first = client.get(f"/analyze?url={url}&vote=real")
    assert first.status_code == 200

    # Same client/IP requesting a different vote should update the existing record.
    second = client.get(f"/analyze?url={url}&vote=ai")
    assert second.status_code == 200

    from veracity.models import ImageRegistry, ImageConsensus, VoteHistory

    with app.app_context():
        with Image.open(io.BytesIO(dummy_image_bytes)) as img:
            target_hash = imagehash.phash(img)
        phash = str(target_hash)

        registry_row = ImageRegistry.query.filter_by(phash=phash).first()
        assert registry_row is not None

        consensus = ImageConsensus.query.filter_by(image_id=registry_row.id).first()
        assert consensus is not None
        assert consensus.vote_real == 0
        assert consensus.vote_ai == 1

        history_rows = VoteHistory.query.filter_by(image_id=registry_row.id).all()
        assert len(history_rows) == 1
        assert history_rows[0].choice == "ai"


def test_vote_creates_record_and_increments_counts(client, app):
    # Arrange: run an analysis to generate a Human Consensus hash
    image_bytes = _make_test_image_bytes()
    data = {
        "file": (io.BytesIO(image_bytes), "test.png"),
        "image_url": "",
    }
    resp = client.post("/analyze", data=data, content_type="multipart/form-data")
    assert resp.status_code == 200

    with Image.open(io.BytesIO(image_bytes)) as img:
        target_hash = imagehash.phash(img)
    phash = str(target_hash)

    vote_data = {"phash": phash, "vote": "real"}
    resp_vote = client.post("/vote", data=vote_data, follow_redirects=False)
    assert resp_vote.status_code == 302
    assert resp_vote.headers["Location"] == "/"

    from veracity.models import ImageRegistry, ImageConsensus, VoteHistory

    with app.app_context():
        registry_row = ImageRegistry.query.filter_by(phash=phash).first()
        assert registry_row is not None

        consensus = ImageConsensus.query.filter_by(image_id=registry_row.id).first()
        assert consensus is not None
        assert consensus.vote_real == 1
        assert consensus.vote_ai == 0

        history_rows = VoteHistory.query.filter_by(image_id=registry_row.id).all()
        assert len(history_rows) == 1
        assert history_rows[0].choice == "real"


def test_vote_redirects_back_to_url_analysis_missing_metadata_falls_back(client):
    image_bytes = _make_test_image_bytes()
    data = {
        "file": (io.BytesIO(image_bytes), "test.png"),
        "image_url": "",
    }
    resp = client.post("/analyze", data=data, content_type="multipart/form-data")
    assert resp.status_code == 200

    with Image.open(io.BytesIO(image_bytes)) as img:
        target_hash = imagehash.phash(img)
    phash = str(target_hash)

    vote_data = {"phash": phash, "vote": "real"}
    resp_vote = client.post("/vote", data=vote_data, follow_redirects=False)
    assert resp_vote.status_code == 302
    assert resp_vote.headers["Location"] == "/"

    # Additional assertion to check the referrer URL
    assert resp_vote.headers["Location"] == "/"

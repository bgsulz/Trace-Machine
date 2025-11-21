import io

from veracity import create_app
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
    assert b"Analysis result" in resp.data
    assert b"Digital Signature (C2PA)" in resp.data
    assert b"Human Consensus" in resp.data


def test_analyze_requires_input(client):
    resp = client.post("/analyze", data={}, content_type="multipart/form-data")
    # should redirect back to index with flash message
    assert resp.status_code == 302
    assert "/" in resp.headers["Location"]


def test_csrf_protection_enabled_by_default():
    # New app where CSRF is active
    app = create_app()
    app.config.update(TESTING=True)
    client = app.test_client()

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

    # Extract the phash from the rendered page
    body = resp.data.decode("utf-8", errors="ignore")
    marker = "<code>"
    start = body.find(marker)
    assert start != -1
    start += len(marker)
    end = body.find("</code>", start)
    phash = body[start:end].strip()
    assert phash

    # Act: submit two votes (one real, one ai)
    vote_data_real = {"phash": phash, "vote": "real"}
    vote_data_ai = {"phash": phash, "vote": "ai"}

    resp_real = client.post("/vote", data=vote_data_real, follow_redirects=True)
    assert resp_real.status_code == 200

    resp_ai = client.post(
        "/vote",
        data=vote_data_ai,
        follow_redirects=True,
        headers={"X-Forwarded-For": "203.0.113.5"},
    )
    assert resp_ai.status_code == 200

    # Assert: database reflects two votes (1 real, 1 ai) and vote history entries
    from veracity.models import ImageConsensus, VoteHistory

    with app.app_context():
        row = ImageConsensus.query.filter_by(phash=phash).first()
        assert row is not None
        assert row.vote_real == 1
        assert row.vote_ai == 1

        history_rows = VoteHistory.query.filter_by(phash=phash).all()
        assert len(history_rows) == 2


def test_vote_rejects_duplicate_votes(client, app):
    image_bytes = _make_test_image_bytes()
    data = {
        "file": (io.BytesIO(image_bytes), "test.png"),
        "image_url": "",
    }
    resp = client.post("/analyze", data=data, content_type="multipart/form-data")
    assert resp.status_code == 200

    body = resp.data.decode("utf-8", errors="ignore")
    marker = "<code>"
    start = body.find(marker)
    assert start != -1
    start += len(marker)
    end = body.find("</code>", start)
    phash = body[start:end].strip()

    vote_data = {"phash": phash, "vote": "real"}

    first = client.post("/vote", data=vote_data, follow_redirects=True)
    assert first.status_code == 200

    second = client.post("/vote", data=vote_data, follow_redirects=True)
    assert second.status_code == 200
    assert b"already voted" in second.data

    from veracity.models import ImageConsensus, VoteHistory

    with app.app_context():
        row = ImageConsensus.query.filter_by(phash=phash).first()
        assert row.vote_real == 1  # not incremented twice
        count = VoteHistory.query.filter_by(phash=phash).count()
        assert count == 1


def test_vote_rejects_invalid_payload(client):
    # Missing phash and vote_kind should redirect back to index
    resp = client.post("/vote", data={}, follow_redirects=False)
    assert resp.status_code == 302
    assert "/" in resp.headers.get("Location", "")

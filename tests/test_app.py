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

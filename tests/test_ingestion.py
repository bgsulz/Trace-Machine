import base64

import pytest
import urllib3

from veracity.ingestion import IngestionError, fetch_image_bytes, validate_image_bytes
from conftest import _make_test_image_bytes


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    """Resolve every host to a public address so tests don't need DNS."""
    monkeypatch.setattr(
        "veracity.safe_fetch.socket.getaddrinfo",
        lambda host, port, **kw: [(2, 1, 6, "", ("93.184.216.34", port or 443))],
    )


def test_validate_image_bytes_rejects_invalid():
    with pytest.raises(IngestionError):
        validate_image_bytes(b"not an image")


def test_fetch_image_bytes_success(app, monkeypatch):
    class DummyResponse:
        status = 200
        headers = {"Content-Type": "image/png"}

        def __init__(self, content: bytes):
            self._content = content

        def stream(self, size=8192):
            yield self._content

        def release_conn(self):
            pass

    dummy_image_bytes = _make_test_image_bytes()

    def fake_get(target):  # noqa: ARG001
        return DummyResponse(dummy_image_bytes)

    monkeypatch.setattr("veracity.safe_fetch._request", fake_get)

    with app.app_context():
        data, mime_type = fetch_image_bytes("https://example.com/image.png")
    assert isinstance(data, (bytes, bytearray))
    assert mime_type.startswith("image/") or mime_type == "image/png"


def test_fetch_image_bytes_non_image_content_type(monkeypatch):
    class DummyResponse:
        status = 200
        headers = {"Content-Type": "text/html"}

        def stream(self, size=8192):  # noqa: ARG002
            yield b"<html></html>"

        def release_conn(self):
            pass

    def fake_get(target):  # noqa: ARG001
        return DummyResponse()

    monkeypatch.setattr("veracity.safe_fetch._request", fake_get)

    with pytest.raises(IngestionError):
        fetch_image_bytes("https://example.com/not-image")


def test_fetch_image_bytes_request_exception(app, monkeypatch):
    def fake_get(target):  # noqa: ARG001
        raise urllib3.exceptions.HTTPError("network error")

    monkeypatch.setattr("veracity.safe_fetch._request", fake_get)

    with app.app_context():
        with pytest.raises(IngestionError):
            fetch_image_bytes("https://example.com/image.png")


def test_fetch_image_bytes_data_url_success(app):
    image_bytes = _make_test_image_bytes()
    data_url = "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")

    with app.app_context():
        data, mime_type = fetch_image_bytes(data_url)

    assert data == image_bytes
    assert mime_type == "image/png"


def test_fetch_image_bytes_data_url_invalid_base64(app):
    data_url = "data:image/png;base64,not_base64"

    with app.app_context():
        with pytest.raises(IngestionError):
            fetch_image_bytes(data_url)

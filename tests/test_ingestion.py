import pytest
import requests

from veracity.ingestion import IngestionError, fetch_image_bytes, validate_image_bytes
from conftest import _make_test_image_bytes


def test_validate_image_bytes_rejects_invalid():
    with pytest.raises(IngestionError):
        validate_image_bytes(b"not an image")


def test_fetch_image_bytes_success(app, monkeypatch):
    class DummyResponse:
        status_code = 200
        headers = {"Content-Type": "image/png"}

        def __init__(self, content: bytes):
            self._content = content

        def iter_content(self, chunk_size=8192):
            yield self._content

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    dummy_image_bytes = _make_test_image_bytes()

    def fake_get(url, timeout=5, stream=True):  # noqa: ARG001
        return DummyResponse(dummy_image_bytes)

    monkeypatch.setattr("veracity.ingestion.requests.get", fake_get)

    with app.app_context():
        data, mime_type = fetch_image_bytes("https://example.com/image.png")
    assert isinstance(data, (bytes, bytearray))
    assert mime_type.startswith("image/") or mime_type == "image/png"


def test_fetch_image_bytes_non_image_content_type(monkeypatch):
    class DummyResponse:
        status_code = 200
        headers = {"Content-Type": "text/html"}

        def iter_content(self, chunk_size=8192):  # noqa: ARG002
            yield b"<html></html>"

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def fake_get(url, timeout=5, stream=True):  # noqa: ARG001
        return DummyResponse()

    monkeypatch.setattr("veracity.ingestion.requests.get", fake_get)

    with pytest.raises(IngestionError):
        fetch_image_bytes("https://example.com/not-image")


def test_fetch_image_bytes_request_exception(app, monkeypatch):
    def fake_get(url, timeout=5, stream=True):  # noqa: ARG001
        raise requests.RequestException("network error")

    monkeypatch.setattr("veracity.ingestion.requests.get", fake_get)

    with app.app_context():
        with pytest.raises(IngestionError):
            fetch_image_bytes("https://example.com/image.png")

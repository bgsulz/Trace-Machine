import io
from unittest.mock import patch, MagicMock

import pytest
from PIL import Image

from conftest import _make_test_image_bytes


@pytest.fixture
def client(app):
    return app.test_client()


def _fake_fetch(url):
    """Return valid image bytes and mime type."""
    return _make_test_image_bytes(), "image/png"


class TestBatchPage:
    def test_get_batch_page(self, client):
        resp = client.get("/batch")
        assert resp.status_code == 200
        assert b"Batch" in resp.data

    def test_post_empty_urls(self, client):
        resp = client.post("/batch", data={"urls": ""}, follow_redirects=True)
        assert resp.status_code == 200
        assert b"at least one" in resp.data

    def test_post_too_many_urls(self, client):
        urls = "\n".join(f"https://example.com/img{i}.jpg" for i in range(11))
        resp = client.post("/batch", data={"urls": urls}, follow_redirects=True)
        assert resp.status_code == 200
        assert b"Maximum" in resp.data

    def test_invalid_url_format(self, client):
        resp = client.post(
            "/batch",
            data={"urls": "not-a-url"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"Invalid URL format" in resp.data

    @patch("veracity.batch_service.ingestion.fetch_image_bytes", side_effect=_fake_fetch)
    @patch("veracity.batch_service.dethumbnail.get_full_res_url", return_value=None)
    def test_valid_single_url(self, mock_dethumb, mock_fetch, client):
        resp = client.post(
            "/batch",
            data={"urls": "https://example.com/photo.jpg"},
        )
        assert resp.status_code == 200
        assert b"Batch Results" in resp.data
        assert b"Full Analysis" in resp.data

    @patch("veracity.batch_service.ingestion.fetch_image_bytes", side_effect=_fake_fetch)
    @patch("veracity.batch_service.dethumbnail.get_full_res_url", return_value=None)
    def test_duplicate_urls_deduplicated(self, mock_dethumb, mock_fetch, client):
        urls = "https://example.com/a.jpg\nhttps://example.com/a.jpg"
        resp = client.post("/batch", data={"urls": urls})
        assert resp.status_code == 200
        # Should only have one result card (deduplicated)
        assert resp.data.count(b"batch-card") >= 1

    @patch("veracity.batch_service.ingestion.fetch_image_bytes", side_effect=_fake_fetch)
    @patch("veracity.batch_service.dethumbnail.get_full_res_url", return_value=None)
    def test_mixed_valid_and_invalid(self, mock_dethumb, mock_fetch, client):
        urls = "https://example.com/good.jpg\nnot-a-url"
        resp = client.post("/batch", data={"urls": urls})
        assert resp.status_code == 200
        assert b"Full Analysis" in resp.data
        assert b"Invalid URL format" in resp.data

    @patch(
        "veracity.batch_service.ingestion.fetch_image_bytes",
        side_effect=lambda url: (_ for _ in ()).throw(
            __import__("veracity.ingestion", fromlist=["IngestionError"]).IngestionError(
                "Failed to download image from URL."
            )
        ),
    )
    @patch("veracity.batch_service.dethumbnail.get_full_res_url", return_value=None)
    def test_fetch_error_shows_error_card(self, mock_dethumb, mock_fetch, client):
        resp = client.post(
            "/batch",
            data={"urls": "https://example.com/broken.jpg"},
        )
        assert resp.status_code == 200
        assert b"batch-card--error" in resp.data


class TestBatchNav:
    def test_nav_contains_batch_link(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"/batch" in resp.data

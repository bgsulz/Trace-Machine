"""Automated OpenAI provenance checks (API mocked; nothing leaves the machine)."""

import html
import io
import json
import re

import pytest
from PIL import Image

from conftest import _make_test_image_bytes
from veracity.services import openai_verify_service as service

POSITIVE = {
    "object": "content_provenance_check",
    "created_at": 1778000000,
    "results": [
        {"type": "c2pa", "outcome": "not_detected", "validation_state": "not_present",
         "issuer": None, "model": None, "generated_at": None},
        {"type": "synthid", "outcome": "detected", "model": "gpt-image", "generated_at": None},
    ],
}
NEGATIVE = {
    "created_at": 1778000000,
    "results": [
        {"type": "c2pa", "outcome": "not_detected", "validation_state": "not_present"},
        {"type": "synthid", "outcome": "not_detected"},
    ],
}


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = body or {}
        self.headers = headers or {}

    def json(self):
        return self._body


@pytest.fixture
def openai_api(app, monkeypatch):
    """Configure a key and capture calls to OpenAI."""
    calls = []
    state = {"response": FakeResponse(200, POSITIVE)}

    def fake_post(url, headers=None, files=None, timeout=None):
        calls.append({"url": url, "headers": headers, "files": files})
        return state["response"]

    app.config["OPENAI_API_KEY"] = "sk-test"
    monkeypatch.setattr(service.requests, "post", fake_post)
    yield {"calls": calls, "state": state}
    app.config.pop("OPENAI_API_KEY", None)


def _upload(client, image_bytes=None, filename="a.png"):
    data = {"file": (io.BytesIO(image_bytes or _make_test_image_bytes()), filename), "image_url": ""}
    resp = client.post("/analyze", data=data, content_type="multipart/form-data")
    return resp.headers["Location"].rsplit("/", 1)[-1]


def test_normalize_response_extracts_signals():
    result = service.normalize_response(POSITIVE)
    assert result["detected"] is True
    assert result["synthid"]["detected"] is True
    assert result["synthid"]["model"] == "gpt-image"
    assert result["c2pa"]["detected"] is False
    assert result["checked_at"].startswith("2026-")


def test_button_hidden_without_api_key(client):
    analysis_id = _upload(client)
    fragment = client.get(f"/analysis/{analysis_id}/analyzers/synthid").data.decode()
    assert "Check automatically" not in fragment
    assert client.post(f"/analysis/{analysis_id}/openai-check").status_code == 404


def test_positive_check_lights_up_portals(client, openai_api):
    analysis_id = _upload(client)
    fragment = client.get(f"/analysis/{analysis_id}/analyzers/synthid").data.decode()
    assert "Check automatically" in fragment
    assert "Sends this image to OpenAI" in fragment

    resp = client.post(f"/analysis/{analysis_id}/openai-check", headers={"HX-Request": "true"})
    body = html.unescape(resp.data.decode())
    assert resp.status_code == 200
    call = openai_api["calls"][0]
    assert call["url"] == service.API_URL
    assert call["headers"]["Authorization"] == "Bearer sk-test"
    assert call["files"]["file"][2] == "image/png"

    assert "Automated check" in body
    assert "OpenAI Verify API" in body
    # The overview chip updates out of band and reads as detected.
    chip = re.search(r'<a\s+class="signal"\s+id="signal-synthid"[^>]*data-state="(\w+)"', body)
    assert chip and chip.group(1) == "found"
    assert "OpenAI's API detected a SynthID watermark on this image." in body
    assert "found OpenAI signals" in json.loads(resp.headers["HX-Trigger"])["showToast"]

    # The stored result persists for later visits.
    again = client.get(f"/analysis/{analysis_id}/analyzers/synthid?refresh=1").data.decode()
    assert "Automated check" in again
    assert "Check again automatically" in again


def test_negative_check_keeps_manual_portals(client, openai_api):
    openai_api["state"]["response"] = FakeResponse(200, NEGATIVE)
    analysis_id = _upload(client)
    body = html.unescape(client.post(f"/analysis/{analysis_id}/openai-check", headers={"HX-Request": "true"}).data.decode())
    assert "Google and Meta still need a manual check" in body
    chip = re.search(r'<a\s+class="signal"\s+id="signal-synthid"[^>]*data-state="(\w+)"', body)
    assert chip and chip.group(1) == "action"


def test_rate_limited_api_shows_a_toast_and_keeps_the_page(client, openai_api):
    openai_api["state"]["response"] = FakeResponse(429, headers={"Retry-After": "30"})
    analysis_id = _upload(client)
    resp = client.post(f"/analysis/{analysis_id}/openai-check", headers={"HX-Request": "true"})
    assert resp.headers["HX-Reswap"] == "none"
    assert "rate-limited" in json.loads(resp.headers["HX-Trigger"])["showToast"]


def test_unsupported_formats_are_sent_as_converted_copies(client, openai_api):
    import pillow_heif  # noqa: F401

    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (10, 120, 200)).save(buf, format="HEIF")
    analysis_id = _upload(client, buf.getvalue(), "IMG.HEIC")
    body = client.post(f"/analysis/{analysis_id}/openai-check", headers={"HX-Request": "true"}).data.decode()
    assert openai_api["calls"][0]["files"]["file"][2] == "image/jpeg"
    assert "converted copy" in body

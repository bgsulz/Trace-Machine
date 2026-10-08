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
    # A positive on this image settles it, so the manual steps collapse.
    assert "Check manually anyway" in again


def test_negative_check_keeps_manual_portals(client, openai_api):
    openai_api["state"]["response"] = FakeResponse(200, NEGATIVE)
    analysis_id = _upload(client)
    body = html.unescape(client.post(f"/analysis/{analysis_id}/openai-check", headers={"HX-Request": "true"}).data.decode())
    assert "Google and Meta still need a manual check" in body
    assert "continue below" in body
    assert "Check manually anyway" not in body
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


def _fresh(payload):
    """The same payload, checked just now (so it's reusable)."""
    import time

    return {**payload, "created_at": int(time.time())}


def _edited_variant(image_bytes):
    """A small local edit: a distinct image that still hashes as a near-duplicate."""
    from PIL import ImageDraw

    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    ImageDraw.Draw(img).rectangle([10, 10, 22, 22], fill=(250, 250, 250))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _photo_bytes():
    """A textured image, so near-identical copies hash as neighbors."""
    import numpy as np

    rng = np.random.default_rng(7)
    base = rng.integers(0, 255, (24, 32, 3), dtype=np.uint8)
    img = Image.fromarray(base).resize((320, 240), Image.BICUBIC)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _synthid_row(client, analysis_id):
    return html.unescape(client.get(f"/analysis/{analysis_id}/analyzers/synthid?refresh=1").data.decode())


def test_positive_on_a_copy_is_only_a_lead_for_this_image(client, openai_api):
    """An AI-edited variant testing positive must not mark the original detected."""
    original = _photo_bytes()
    variant_id = _upload(client, _edited_variant(original), "variant.png")
    client.post(f"/analysis/{variant_id}/openai-check", headers={"HX-Request": "true"})

    original_id = _upload(client, original, "original.png")
    row = _synthid_row(client, original_id)
    pills = re.findall(r'class="pill" data-state="(\w+)"><span class="dot"></span>([^<]*)', row)
    assert pills[0] == ("found", "Reported")  # a lead, not a detection
    assert "OpenAI's API detected a SynthID watermark on a near-identical copy." in row
    assert "check this image itself" in row


def test_invalid_openai_manifest_does_not_count():
    payload = {"results": [{"type": "c2pa", "outcome": "detected", "validation_state": "invalid"},
                           {"type": "synthid", "outcome": "not_detected"}]}
    result = service.normalize_response(payload)
    assert result["detected"] is False
    assert result["c2pa"]["present_but_invalid"] is True


def test_malformed_responses_dont_crash():
    for payload in (None, [], {"results": "x"}, {"results": [1, None, {"type": "c2pa"}]},
                    {"results": [], "created_at": 10**30}, {"created_at": True}):
        result = service.normalize_response(payload)
        assert result["detected"] is False
        assert result["checked_at"]


def test_automated_negative_overrides_community_openai_reports(app, client, openai_api):
    openai_api["state"]["response"] = FakeResponse(200, NEGATIVE)
    analysis_id = _upload(client)
    for _ in range(5):  # five different visitors report an OpenAI positive
        other = app.test_client()
        other.post("/synthid-report", data={"analysis_id": analysis_id, "report": "openai_positive"})
    assert "reported a portal naming OpenAI as the maker" in _synthid_row(client, analysis_id)

    client.post(f"/analysis/{analysis_id}/openai-check", headers={"HX-Request": "true"})
    row = _synthid_row(client, analysis_id)
    assert "reported a portal naming OpenAI as the maker" not in row
    assert "found no OpenAI signals" in row


def test_only_the_uploader_can_send_an_upload(app, client, openai_api):
    analysis_id = _upload(client)
    stranger = app.test_client()
    page = stranger.get(f"/analysis/{analysis_id}/analyzers/synthid").data.decode()
    assert "Check automatically" not in page
    resp = stranger.post(f"/analysis/{analysis_id}/openai-check", headers={"HX-Request": "true"})
    assert "Only the person who uploaded" in json.loads(resp.headers["HX-Trigger"])["showToast"]
    assert openai_api["calls"] == []


def test_recent_results_are_reused_instead_of_calling_again(client, openai_api):
    openai_api["state"]["response"] = FakeResponse(200, _fresh(POSITIVE))
    analysis_id = _upload(client)
    client.post(f"/analysis/{analysis_id}/openai-check", headers={"HX-Request": "true"})
    # A fresh result hides the button; a direct re-post reuses the stored result.
    assert "Check again automatically" not in _synthid_row(client, analysis_id)
    resp = client.post(f"/analysis/{analysis_id}/openai-check", headers={"HX-Request": "true"})
    assert len(openai_api["calls"]) == 1
    assert "checked recently" in json.loads(resp.headers["HX-Trigger"])["showToast"]


def test_malformed_analysis_ids_are_not_found(client):
    for bad in ("..%5C..%5Cetc", "not-an-id", "DEADBEEF" * 4):
        assert client.get(f"/analysis/{bad}/raw").status_code == 404

import io
import json
import re

from conftest import _make_test_image_bytes
from veracity.analysis_cache import (
    analysis_row_path,
    store_analysis_payload,
    store_cached_analyzer_row,
)
from veracity.analyzers.manager import ANALYZERS


def _extract_analysis_id(html: str) -> str:
    match = re.search(r"/analysis/([a-f0-9]+)/analyzers/", html)
    assert match is not None
    return match.group(1)


def _seed_analysis_with_rows(app) -> str:
    image_bytes = _make_test_image_bytes(size=(24, 24))
    metadata = {
        "mime_type": "image/png",
        "source": "file",
        "image_url": None,
        "public_url": None,
        "analysis_link": None,
        "phash": "phash-fixture",
        "whash": "whash-fixture",
        "registry_id": 123,
        "crop_box": [0.1, 0.2, 0.3, 0.4],
        "full_res_url": None,
        "image_width": 24,
        "image_height": 24,
        "public_url_display": None,
        "created_at": 1_700_000_000,
    }
    with app.app_context():
        analysis_id = store_analysis_payload("exportfixture001", image_bytes, metadata)
        for index, spec in enumerate(ANALYZERS):
            row = {
                "name": spec.name,
                "slug": spec.slug,
                "status": "FOUND" if index % 2 == 0 else "NOT FOUND",
                "summary": f"summary for {spec.slug}",
                "data": {"fixture_index": index},
                "template": spec.template,
            }
            store_cached_analyzer_row(analysis_id, spec.slug, row)
    return analysis_id


def test_result_page_links_export_actions(client):
    image_bytes = _make_test_image_bytes()
    data = {
        "file": (io.BytesIO(image_bytes), "test.png"),
        "image_url": "",
    }
    resp = client.post("/analyze", data=data, content_type="multipart/form-data")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    analysis_id = _extract_analysis_id(body)
    assert f"/analysis/{analysis_id}/export.json" in body
    assert f"/analysis/{analysis_id}/export.html" in body
    assert "Download JSON" in body
    assert "Print Report" in body


def test_export_json_has_expected_shape_and_types(client, app):
    analysis_id = _seed_analysis_with_rows(app)
    resp = client.get(f"/analysis/{analysis_id}/export.json")
    assert resp.status_code == 200
    assert resp.headers.get("Content-Type", "").startswith("application/json")

    payload = json.loads(resp.data.decode("utf-8"))
    assert isinstance(payload.get("report_version"), str)
    assert isinstance(payload.get("generated_at"), str)
    assert "app_version" in payload

    analysis = payload.get("analysis")
    assert isinstance(analysis, dict)
    assert analysis.get("id") == analysis_id
    assert isinstance(analysis.get("created_at"), str)
    assert isinstance(analysis.get("dimensions"), dict)
    assert isinstance(analysis["dimensions"].get("width"), int)
    assert isinstance(analysis["dimensions"].get("height"), int)
    assert isinstance(analysis.get("crop_box"), dict)

    hashes = payload.get("hashes")
    assert isinstance(hashes, dict)
    assert isinstance(hashes.get("phash"), str)
    assert isinstance(hashes.get("whash"), str)
    assert isinstance(hashes.get("registry_id"), int)

    analyzers = payload.get("analyzers")
    assert isinstance(analyzers, list)
    assert len(analyzers) == len(ANALYZERS)
    assert [row["slug"] for row in analyzers] == [spec.slug for spec in ANALYZERS]
    for row in analyzers:
        assert set(row.keys()) == {"name", "slug", "status", "summary", "data", "template"}
        assert isinstance(row["name"], str)
        assert isinstance(row["slug"], str)
        assert isinstance(row["status"], str)
        assert isinstance(row["summary"], str)
        assert isinstance(row["data"], dict)
        assert isinstance(row["template"], str)

    tools = payload.get("tools")
    assert isinstance(tools, list)
    assert tools
    assert isinstance(tools[0].get("name"), str)
    assert isinstance(tools[0].get("links"), list)


def test_export_html_renders_printable_report(client, app):
    analysis_id = _seed_analysis_with_rows(app)
    resp = client.get(f"/analysis/{analysis_id}/export.html")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    assert "Veracity Provenance Report" in body
    assert "Generated at:" in body
    assert "Analyzers" in body
    assert "summary for c2pa" in body


def test_export_json_includes_not_run_for_missing_cached_row(client, app):
    analysis_id = _seed_analysis_with_rows(app)
    missing_slug = ANALYZERS[0].slug
    with app.app_context():
        analysis_row_path(analysis_id, missing_slug).unlink(missing_ok=True)

    resp = client.get(f"/analysis/{analysis_id}/export.json")
    assert resp.status_code == 200
    payload = json.loads(resp.data.decode("utf-8"))
    row = next(item for item in payload["analyzers"] if item["slug"] == missing_slug)
    assert row["status"] == "NOT_RUN"
    assert row["summary"] == "Analyzer row is not available yet."
    assert row["data"] == {}


def test_export_routes_use_expired_analysis_response(client):
    for suffix in ("export.json", "export.html"):
        resp = client.get(f"/analysis/missing-analysis/{suffix}")
        assert resp.status_code == 410
        assert resp.headers.get("Location", "").endswith("/")

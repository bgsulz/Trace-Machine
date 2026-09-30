"""Remote C2PA manifests: fetched safely by us, never by the c2pa library."""

import http.server
import socketserver
import struct
import threading
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from veracity.analyzers.c2pa import _run_c2pa_tool

SIGNED_PNG = Path(__file__).resolve().parents[1] / "veracity/static/info_images/infrared_tree.png"


def _png_chunks(png: bytes):
    pos = 8
    while pos < len(png):
        length = struct.unpack(">I", png[pos : pos + 4])[0]
        yield png[pos + 4 : pos + 8], png[pos + 8 : pos + 8 + length]
        pos += 12 + length


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def _split_manifest(manifest_url: str) -> tuple[bytes, bytes]:
    """Move the sample's embedded manifest out, leaving an XMP pointer to it."""
    png = SIGNED_PNG.read_bytes()
    manifest = b"".join(data for kind, data in _png_chunks(png) if kind == b"caBX")
    xmp = (
        '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF '
        'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description '
        f'xmlns:dcterms="http://purl.org/dc/terms/" dcterms:provenance="{manifest_url}"/>'
        "</rdf:RDF></x:xmpmeta>"
    ).encode()
    out = png[:8]
    for kind, data in _png_chunks(png):
        if kind == b"caBX":
            continue
        if kind == b"IDAT" and b"iTXt" not in out:
            out += _chunk(b"iTXt", b"XML:com.adobe.xmp\x00\x00\x00\x00\x00" + xmp)
        out += _chunk(kind, data)
    return out, manifest


@pytest.fixture
def manifest_server():
    """A local HTTP server; records every request it receives."""
    state = {"hits": [], "body": b"", "status": 200}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            state["hits"].append(self.path)
            self.send_response(state["status"])
            self.send_header("Content-Type", "application/c2pa")
            self.end_headers()
            if state["status"] == 200:
                self.wfile.write(state["body"])

        def log_message(self, *args):
            pass

    server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state["url"] = f"http://127.0.0.1:{server.server_address[1]}/manifest.c2pa"
    yield state
    server.shutdown()
    server.server_close()


def _analyze_in_worker_thread(app, image_bytes):
    # Analyzers run in a thread pool, and c2pa settings are per-thread.
    def run():
        with app.app_context():
            return _run_c2pa_tool(image_bytes)

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(run).result()


def test_private_manifest_url_is_never_requested(app, manifest_server):
    image, _ = _split_manifest(manifest_server["url"])
    result = _analyze_in_worker_thread(app, image)

    assert manifest_server["hits"] == []  # neither c2pa nor we fetched it
    assert result["status"] == "REFERENCED"
    assert result["data"]["remote_manifest_url"] == manifest_server["url"]
    assert "public internet" in result["data"]["remote_error"]


def test_remote_manifest_is_fetched_and_validated(app, manifest_server):
    app.config["SAFE_FETCH_ALLOW_PRIVATE"] = True  # the test server is local
    try:
        image, manifest = _split_manifest(manifest_server["url"])
        manifest_server["body"] = manifest
        result = _analyze_in_worker_thread(app, image)
    finally:
        app.config["SAFE_FETCH_ALLOW_PRIVATE"] = False

    assert manifest_server["hits"] == ["/manifest.c2pa"]  # exactly once, by us
    assert result["status"] == "FOUND"
    assert result["data"]["remote_manifest_url"] == manifest_server["url"]
    assert result["data"]["producer"] == "ChatGPT (OpenAI)"


def test_unreachable_remote_manifest_is_reported(app, manifest_server):
    app.config["SAFE_FETCH_ALLOW_PRIVATE"] = True
    manifest_server["status"] = 404
    try:
        image, _ = _split_manifest(manifest_server["url"])
        result = _analyze_in_worker_thread(app, image)
    finally:
        app.config["SAFE_FETCH_ALLOW_PRIVATE"] = False

    assert result["status"] == "REFERENCED"
    assert "404" in result["data"]["remote_error"]

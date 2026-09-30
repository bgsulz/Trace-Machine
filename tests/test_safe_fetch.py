"""Server-side request forgery protections for user-supplied URLs."""


import pytest

from veracity.safe_fetch import FetchError, UnsafeURLError, check_url, safe_get


def _resolve_to(monkeypatch, address):
    monkeypatch.setattr(
        "veracity.safe_fetch.socket.getaddrinfo",
        lambda host, port, **kw: [(2, 1, 6, "", (address, port or 80))],
    )


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.0.0.5", "192.168.1.1", "172.16.0.1", "169.254.169.254",
     "100.64.0.1", "0.0.0.0", "::1", "fd00::1", "::ffff:127.0.0.1"],
)
def test_non_public_addresses_are_refused(monkeypatch, address):
    _resolve_to(monkeypatch, address)
    with pytest.raises(UnsafeURLError):
        check_url("http://innocent-looking.example/image.png")


def test_public_address_is_allowed(monkeypatch):
    _resolve_to(monkeypatch, "93.184.216.34")
    check_url("https://example.com/image.png")


@pytest.mark.parametrize(
    "url",
    ["file:///etc/passwd", "ftp://example.com/x", "http://user:pw@example.com/x", "http:///nohost"],
)
def test_bad_schemes_and_credentials_are_refused(url):
    with pytest.raises(UnsafeURLError):
        check_url(url)


def test_redirects_are_rechecked_at_every_hop(monkeypatch):
    """A public URL that redirects to an internal address is refused."""

    class Redirect:
        status_code = 302
        is_redirect = True
        headers = {"Location": "http://metadata.internal/latest"}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr("veracity.safe_fetch.requests.get", lambda url, **kw: Redirect())
    monkeypatch.setattr(
        "veracity.safe_fetch.socket.getaddrinfo",
        lambda host, port, **kw: [
            (2, 1, 6, "", ("169.254.169.254" if host == "metadata.internal" else "93.184.216.34", 80))
        ],
    )
    with pytest.raises(UnsafeURLError):
        safe_get("http://public.example/start", max_bytes=1000)


def test_size_limit_is_enforced(monkeypatch):
    class Big:
        status_code = 200
        is_redirect = False
        headers = {"Content-Type": "image/png"}

        def iter_content(self, chunk_size=8192):
            for _ in range(10):
                yield b"x" * 1000

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    _resolve_to(monkeypatch, "93.184.216.34")
    monkeypatch.setattr("veracity.safe_fetch.requests.get", lambda url, **kw: Big())
    with pytest.raises(FetchError):
        safe_get("https://example.com/big", max_bytes=5000)


def test_analyze_url_refuses_internal_addresses(client, monkeypatch):
    _resolve_to(monkeypatch, "169.254.169.254")
    resp = client.get("/analyze?url=http://metadata.example/latest/meta-data")
    assert resp.status_code == 302  # back home with a message, nothing fetched


def test_own_static_files_are_read_from_disk(app):
    """Sample images link to our own (often localhost) host."""
    from veracity.ingestion import fetch_image_bytes

    with app.test_request_context("/", base_url="http://localhost.localdomain"):
        data, mime = fetch_image_bytes("http://localhost.localdomain/static/info_images/bottle.png")
    assert mime == "image/png"
    assert data[:8] == b"\x89PNG\r\n\x1a\n"

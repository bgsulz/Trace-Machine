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


class FakeResponse:
    def __init__(self, status=200, headers=None, chunks=()):
        self.status = status
        self.headers = headers or {}
        self._chunks = chunks

    def stream(self, size):
        yield from self._chunks

    def release_conn(self):
        pass


def test_redirects_are_rechecked_at_every_hop(monkeypatch):
    """A public URL that redirects to an internal address is refused."""
    monkeypatch.setattr(
        "veracity.safe_fetch._request",
        lambda target: FakeResponse(302, {"Location": "http://metadata.internal/latest"}),
    )
    monkeypatch.setattr(
        "veracity.safe_fetch.socket.getaddrinfo",
        lambda host, port, **kw: [
            (2, 1, 6, "", ("169.254.169.254" if host == "metadata.internal" else "93.184.216.34", 80))
        ],
    )
    with pytest.raises(UnsafeURLError):
        safe_get("http://public.example/start", max_bytes=1000)


def test_size_limit_is_enforced(monkeypatch):
    _resolve_to(monkeypatch, "93.184.216.34")
    monkeypatch.setattr(
        "veracity.safe_fetch._request",
        lambda target: FakeResponse(200, {"Content-Type": "image/png"}, [b"x" * 1000] * 10),
    )
    with pytest.raises(FetchError):
        safe_get("https://example.com/big", max_bytes=5000)


def test_connects_to_the_checked_address(monkeypatch):
    """The connection uses the address that passed the check (no re-resolve)."""
    _resolve_to(monkeypatch, "93.184.216.34")
    seen = {}

    def fake_request(target):
        seen.update(address=target.address, host=target.host, path=target.path)
        return FakeResponse(200, {"Content-Type": "image/png"}, [b"ok"])

    monkeypatch.setattr("veracity.safe_fetch._request", fake_request)
    result = safe_get("https://example.com/a.png?x=1", max_bytes=100)
    assert result.content == b"ok"
    assert seen == {"address": "93.184.216.34", "host": "example.com", "path": "/a.png?x=1"}


def test_slow_downloads_hit_the_deadline(monkeypatch):
    _resolve_to(monkeypatch, "93.184.216.34")
    clock = iter(range(0, 1000, 5))
    monkeypatch.setattr("veracity.safe_fetch.time.monotonic", lambda: next(clock))
    monkeypatch.setattr(
        "veracity.safe_fetch._request",
        lambda target: FakeResponse(200, {}, [b"x"] * 100),
    )
    with pytest.raises(FetchError, match="too long"):
        safe_get("https://example.com/slow", max_bytes=10_000)


@pytest.mark.parametrize(
    "address",
    ["64:ff9b::a9fe:a9fe", "64:ff9b::7f00:1", "::127.0.0.1", "fec0::1", "64:ff9b:1::1"],
)
def test_ipv6_forms_hiding_internal_addresses_are_refused(monkeypatch, address):
    _resolve_to(monkeypatch, address)
    with pytest.raises(UnsafeURLError):
        check_url("http://sneaky.example/")


def test_bad_ports_are_refused_cleanly(monkeypatch):
    _resolve_to(monkeypatch, "93.184.216.34")
    for url in ("http://example.com:99999/", "http://example.com:22/"):
        with pytest.raises(UnsafeURLError):
            check_url(url)


def test_unresolvable_and_private_hosts_look_the_same(monkeypatch):
    import socket

    def fail(*args, **kwargs):
        raise socket.gaierror("nope")

    monkeypatch.setattr("veracity.safe_fetch.socket.getaddrinfo", fail)
    with pytest.raises(UnsafeURLError) as unresolvable:
        check_url("http://db.internal/")
    _resolve_to(monkeypatch, "10.0.0.5")
    with pytest.raises(UnsafeURLError) as private:
        check_url("http://db2.internal/")
    assert str(unresolvable.value) == str(private.value)


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

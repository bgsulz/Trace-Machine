"""Outbound HTTP for user-supplied URLs, without server-side request forgery.

Every URL the server fetches on a visitor's behalf (images to analyze, remote
C2PA manifests named inside an image) goes through ``safe_get``:

- the host is resolved once and every address must be public (no loopback,
  private, link-local, CGNAT, metadata-service, or NAT64/IPv4-compatible
  forms that smuggle those in);
- the connection is made to that exact checked address, with the original
  hostname used for the ``Host`` header and TLS verification, so DNS can't
  answer differently at connect time (rebinding);
- only ports 80/443/8080/8443, no environment proxies or ``.netrc``;
- redirects are followed manually, each hop checked the same way;
- bodies are size-capped and the whole download has a deadline.
"""

from __future__ import annotations

import ipaddress
import socket
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import certifi
import urllib3
from flask import current_app, has_app_context

MAX_REDIRECTS = 5
READ_TIMEOUT = 5  # seconds per connect/read
TOTAL_DEADLINE = 20  # seconds for the whole download, including slow drips
ALLOWED_PORTS = {80, 443, 8080, 8443}
USER_AGENT = "TraceMachine/1.0 (+provenance checker)"
NOT_PUBLIC_MESSAGE = "That address isn't reachable on the public internet."

_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_BLOCKED_V6 = (
    ipaddress.ip_network("64:ff9b:1::/48"),  # local-use NAT64
    ipaddress.ip_network("::/96"),  # deprecated IPv4-compatible
    ipaddress.ip_network("fec0::/10"),  # deprecated site-local
)


class UnsafeURLError(Exception):
    """The URL points somewhere the server must not fetch."""


class FetchError(Exception):
    """The fetch failed (network error, bad status, too large, or too slow)."""


@dataclass
class FetchResult:
    url: str  # final URL after redirects
    content: bytes
    content_type: str


@dataclass
class _Target:
    scheme: str
    host: str  # hostname as written, for Host/SNI
    port: int
    address: str  # the checked IP we connect to
    path: str


def _allow_private() -> bool:
    return bool(has_app_context() and current_app.config.get("SAFE_FETCH_ALLOW_PRIVATE"))


def _is_public_address(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        elif ip in _NAT64:
            ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        elif any(ip in net for net in _BLOCKED_V6):
            return False
    return ip.is_global and not ip.is_multicast


def _resolve(url: str) -> _Target:
    """Validate *url* and pick a checked address to connect to."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise UnsafeURLError("Only HTTP and HTTPS URLs can be fetched.")
    try:
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        raise UnsafeURLError("The URL's host or port is invalid.") from None
    if not host:
        raise UnsafeURLError("URL has no host.")
    if parsed.username or parsed.password:
        raise UnsafeURLError("URLs with credentials aren't allowed.")

    allow_private = _allow_private()
    if port not in ALLOWED_PORTS and not allow_private:
        raise UnsafeURLError("Only standard web ports can be fetched.")

    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError):
        # Same message as a private address, so internal names can't be probed.
        raise UnsafeURLError(NOT_PUBLIC_MESSAGE) from None
    addresses = [info[4][0] for info in infos]
    if not addresses or not (allow_private or all(_is_public_address(a) for a in addresses)):
        raise UnsafeURLError(NOT_PUBLIC_MESSAGE)

    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    return _Target(parsed.scheme, host, port, addresses[0].split("%", 1)[0], path)


def check_url(url: str) -> None:
    """Raise UnsafeURLError unless *url* is safe to fetch."""
    _resolve(url)


def _request(target: _Target) -> urllib3.BaseHTTPResponse:
    """GET *target* from its checked address (never re-resolving the name)."""
    timeout = urllib3.Timeout(connect=READ_TIMEOUT, read=READ_TIMEOUT)
    if target.scheme == "https":
        pool = urllib3.HTTPSConnectionPool(
            target.address, target.port, timeout=timeout, retries=False,
            cert_reqs="CERT_REQUIRED", ca_certs=certifi.where(),
            server_hostname=target.host, assert_hostname=target.host,
        )
    else:
        pool = urllib3.HTTPConnectionPool(
            target.address, target.port, timeout=timeout, retries=False,
        )
    default_port = 443 if target.scheme == "https" else 80
    host = f"[{target.host}]" if ":" in target.host else target.host  # IPv6 literal
    host_header = host if target.port == default_port else f"{host}:{target.port}"
    return pool.urlopen(
        "GET", target.path,
        headers={"Host": host_header, "User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"},
        redirect=False, preload_content=False, decode_content=True,
    )


def safe_get(url: str, *, max_bytes: int) -> FetchResult:
    """GET *url*, following up to MAX_REDIRECTS redirects, each re-checked.

    Raises UnsafeURLError for disallowed destinations and FetchError for
    network failures, non-200 responses, oversize bodies, or slow downloads.
    """
    deadline = time.monotonic() + TOTAL_DEADLINE
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        target = _resolve(current)
        try:
            response = _request(target)
        except urllib3.exceptions.HTTPError:
            raise FetchError("Couldn't download the URL.") from None

        try:
            if 300 <= response.status < 400 and response.headers.get("Location"):
                current = urljoin(current, response.headers["Location"])
                continue
            if response.status != 200:
                raise FetchError(f"The URL returned HTTP {response.status}.")

            declared = response.headers.get("Content-Length", "")
            if declared.isdigit() and int(declared) > max_bytes:
                raise FetchError("The download is too large.")
            body = bytearray()
            try:
                for chunk in response.stream(8192):
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise FetchError("The download is too large.")
                    if time.monotonic() > deadline:
                        raise FetchError("The download took too long.")
            except urllib3.exceptions.HTTPError:
                raise FetchError("Couldn't download the URL.") from None
            return FetchResult(
                url=current,
                content=bytes(body),
                content_type=response.headers.get("Content-Type", ""),
            )
        finally:
            response.release_conn()

    raise FetchError("Too many redirects.")

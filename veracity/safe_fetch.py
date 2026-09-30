"""Outbound HTTP for user-supplied URLs, without server-side request forgery.

Every URL the server fetches on a visitor's behalf (images to analyze, remote
C2PA manifests named inside an image) goes through ``safe_get``. It refuses
hosts that resolve to private, loopback, link-local, or otherwise non-public
addresses (cloud metadata services, the app's own admin ports, the LAN), and
follows redirects manually so every hop is checked the same way.

Residual risk: a hostname can resolve differently between our check and the
connection (DNS rebinding). The window is small, and responses are never
reflected verbatim, but a deployment on a sensitive network should also
restrict egress at the network layer.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import requests
from flask import current_app, has_app_context

MAX_REDIRECTS = 5
DEFAULT_TIMEOUT = 5  # seconds, per connect/read


class UnsafeURLError(Exception):
    """The URL points somewhere the server must not fetch."""


class FetchError(Exception):
    """The fetch failed (network error, bad status, or too large)."""


@dataclass
class FetchResult:
    url: str  # final URL after redirects
    content: bytes
    content_type: str


def _allow_private() -> bool:
    return bool(has_app_context() and current_app.config.get("SAFE_FETCH_ALLOW_PRIVATE"))


def _is_public_address(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def check_url(url: str) -> None:
    """Raise UnsafeURLError unless *url* is an http(s) URL on a public host."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise UnsafeURLError("Only HTTP and HTTPS URLs can be fetched.")
    if not parsed.hostname:
        raise UnsafeURLError("URL has no host.")
    if parsed.username or parsed.password:
        raise UnsafeURLError("URLs with credentials aren't allowed.")
    if _allow_private():
        return

    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError):
        raise UnsafeURLError("Couldn't resolve the URL's host.") from None
    addresses = {info[4][0] for info in infos}
    if not addresses or not all(_is_public_address(a) for a in addresses):
        raise UnsafeURLError("That address isn't on the public internet.")


def safe_get(url: str, *, max_bytes: int, timeout: float = DEFAULT_TIMEOUT) -> FetchResult:
    """GET *url*, following up to MAX_REDIRECTS redirects, each re-checked.

    Raises UnsafeURLError for disallowed destinations and FetchError for
    network failures, non-200 responses, or bodies larger than *max_bytes*.
    """
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        check_url(current)
        try:
            response = requests.get(
                current, timeout=timeout, stream=True, allow_redirects=False,
                headers={"User-Agent": "TraceMachine/1.0 (+provenance checker)"},
            )
        except requests.RequestException:
            raise FetchError("Couldn't download the URL.") from None

        with response:
            if response.is_redirect:
                location = response.headers.get("Location")
                if not location:
                    raise FetchError("Redirect without a destination.")
                current = urljoin(current, location)
                continue
            if response.status_code != 200:
                raise FetchError(f"The URL returned HTTP {response.status_code}.")

            declared = response.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise FetchError("The download is too large.")
            body = bytearray()
            try:
                for chunk in response.iter_content(chunk_size=8192):
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise FetchError("The download is too large.")
            except requests.RequestException:
                raise FetchError("Couldn't download the URL.") from None
            return FetchResult(
                url=current,
                content=bytes(body),
                content_type=response.headers.get("Content-Type", ""),
            )

    raise FetchError("Too many redirects.")

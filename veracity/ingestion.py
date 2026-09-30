import base64
import binascii
from io import BytesIO
from urllib.parse import urlparse, unquote_to_bytes

import requests
from flask import current_app, has_app_context
from PIL import Image, UnidentifiedImageError


class IngestionError(Exception):
    pass


_FORMAT_MIME_TYPES = {
    "JPEG": "image/jpeg",
    # Multi-picture JPEGs (Ultra HDR / gain-map photos from Pixel, Samsung,
    # iPhone) are ordinary JPEGs as far as browsers and C2PA are concerned.
    "MPO": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
    "GIF": "image/gif",
    "AVIF": "image/avif",
    "HEIF": "image/heic",
    "TIFF": "image/tiff",
    "BMP": "image/bmp",
}

# Formats every mainstream browser can display as-is.
BROWSER_DISPLAYABLE = {
    "image/jpeg", "image/png", "image/webp", "image/gif", "image/avif", "image/bmp",
}


def sniff_mime_type(data: bytes, fallback: str | None = None) -> str:
    """Return the MIME type of image bytes, preferring what Pillow detects.

    Browser-supplied and server-supplied types are unreliable (Windows often
    sends HEIC as application/octet-stream), so they're only a fallback, and
    only an ``image/*`` fallback is ever returned: the type is later used to
    serve the bytes back, and e.g. ``text/html`` there would be stored XSS.
    """
    try:
        with Image.open(BytesIO(data)) as img:
            detected = _FORMAT_MIME_TYPES.get((img.format or "").upper())
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        detected = None
    if detected:
        return detected
    fallback = (fallback or "").split(";")[0].strip().lower()
    if fallback.startswith("image/") and "svg" not in fallback:
        return fallback
    return "application/octet-stream"


def validate_image_bytes(data: bytes) -> None:
    """Validate that the given bytes represent a loadable image.

    Raises IngestionError if the bytes are not a valid image.
    """
    try:
        with Image.open(BytesIO(data)) as img:
            img.verify()
    except (UnidentifiedImageError, OSError):
        raise IngestionError("Provided file is not a valid image.") from None


def fetch_image_bytes(url: str) -> tuple[bytes, str]:
    """Download an image from a URL into memory and validate it.

    Returns (image_bytes, mime_type).
    """
    parsed = urlparse(url)
    default_max_bytes = 10 * 1024 * 1024
    if has_app_context():
        max_bytes = current_app.config.get("MAX_CONTENT_LENGTH", default_max_bytes)
    else:
        max_bytes = default_max_bytes

    if parsed.scheme == "data":
        try:
            header, data_part = parsed.path.split(",", 1)
        except ValueError:
            raise IngestionError("Invalid data URL.") from None

        media_type = "application/octet-stream"
        params = [segment for segment in header.split(";") if segment]
        base64_encoded = False
        if params:
            media_type = params[0] or media_type
            base64_encoded = any(part.lower() == "base64" for part in params[1:])

        if base64_encoded:
            try:
                data_bytes = base64.b64decode(data_part, validate=True)
            except (binascii.Error, ValueError):
                raise IngestionError("Invalid base64 data URL.") from None
        else:
            data_bytes = unquote_to_bytes(data_part)

        if len(data_bytes) > max_bytes:
            raise IngestionError("Provided data URL is too large.")

        validate_image_bytes(data_bytes)
        return data_bytes, sniff_mime_type(data_bytes, media_type)

    if parsed.scheme not in {"http", "https"}:
        raise IngestionError("Only HTTP/HTTPS URLs are supported.")

    try:
        with requests.get(url, timeout=5, stream=True) as resp:
            if resp.status_code != 200:
                raise IngestionError("Image URL returned a non-200 status code.")

            content_type = resp.headers.get("Content-Type", "")
            if "image" not in content_type:
                raise IngestionError("URL does not point to an image.")

            content_length = resp.headers.get("Content-Length")
            if content_length is not None:
                try:
                    length_val = int(content_length)
                except ValueError:
                    length_val = None
                else:
                    if length_val > max_bytes:
                        raise IngestionError("Downloaded image is too large.")

            buf = bytearray()

            for chunk in resp.iter_content(chunk_size=8192):
                if not chunk:
                    continue
                if len(buf) + len(chunk) > max_bytes:
                    raise IngestionError("Downloaded image is too large.")
                buf.extend(chunk)

    except requests.RequestException:
        raise IngestionError("Failed to download image from URL.") from None

    data = bytes(buf)
    validate_image_bytes(data)

    return data, sniff_mime_type(data, content_type)

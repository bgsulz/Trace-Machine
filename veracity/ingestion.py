from io import BytesIO
from urllib.parse import urlparse

import requests
from flask import current_app
from PIL import Image, UnidentifiedImageError


class IngestionError(Exception):
    pass


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
    if parsed.scheme not in {"http", "https"}:
        raise IngestionError("Only HTTP/HTTPS URLs are supported.")

    try:
        with requests.get(url, timeout=5, stream=True) as resp:
            if resp.status_code != 200:
                raise IngestionError("Image URL returned a non-200 status code.")

            content_type = resp.headers.get("Content-Type", "")
            if "image" not in content_type:
                raise IngestionError("URL does not point to an image.")

            max_bytes = current_app.config.get("MAX_CONTENT_LENGTH", 10 * 1024 * 1024)
            buf = bytearray()

            for chunk in resp.iter_content(chunk_size=8192):
                if not chunk:
                    continue
                buf.extend(chunk)
                if len(buf) > max_bytes:
                    raise IngestionError("Downloaded image is too large.")

    except requests.RequestException:
        raise IngestionError("Failed to download image from URL.") from None

    data = bytes(buf)
    validate_image_bytes(data)

    mime_type = content_type or "application/octet-stream"
    return data, mime_type

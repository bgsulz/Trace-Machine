import os
import secrets
from urllib.parse import urlparse

import requests
from flask import current_app
from PIL import Image, UnidentifiedImageError


class IngestionError(Exception):
    pass


def download_image_to_uploads(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise IngestionError("Only HTTP/HTTPS URLs are supported.")

    try:
        resp = requests.get(url, timeout=5, stream=True)
    except requests.RequestException:
        raise IngestionError("Failed to download image from URL.") from None

    if resp.status_code != 200:
        raise IngestionError("Image URL returned a non-200 status code.")

    content_type = resp.headers.get("Content-Type", "")
    if "image" not in content_type:
        raise IngestionError("URL does not point to an image.")

    upload_folder = current_app.config["UPLOAD_FOLDER"]
    os.makedirs(upload_folder, exist_ok=True)

    ext = _guess_extension_from_content_type(content_type)
    random_name = secrets.token_hex(16)
    stored_name = f"{random_name}{ext}" if ext else random_name
    path = os.path.join(upload_folder, stored_name)

    size = 0
    max_bytes = current_app.config.get("MAX_CONTENT_LENGTH", 10 * 1024 * 1024)

    with open(path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            if not chunk:
                continue
            size += len(chunk)
            if size > max_bytes:
                f.close()
                os.remove(path)
                raise IngestionError("Downloaded image is too large.")
            f.write(chunk)

    try:
        with Image.open(path) as img:
            img.verify()
    except (UnidentifiedImageError, OSError):
        os.remove(path)
        raise IngestionError("Downloaded file is not a valid image.") from None

    return stored_name


def _guess_extension_from_content_type(content_type: str) -> str:
    if "jpeg" in content_type:
        return ".jpg"
    if "png" in content_type:
        return ".png"
    if "webp" in content_type:
        return ".webp"
    if "gif" in content_type:
        return ".gif"
    return ""

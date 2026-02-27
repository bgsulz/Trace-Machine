from __future__ import annotations

from dataclasses import dataclass

from . import dethumbnail, ingestion


@dataclass(frozen=True, slots=True)
class RemoteImageFetchResult:
    image_bytes: bytes
    mime_type: str
    fetch_url: str
    full_res_url: str | None
    upgraded: bool


def fetch_remote_image(image_url: str) -> RemoteImageFetchResult:
    full_res_url = dethumbnail.get_full_res_url(image_url)
    if full_res_url:
        try:
            image_bytes, mime_type = ingestion.fetch_image_bytes(full_res_url)
            return RemoteImageFetchResult(
                image_bytes=image_bytes,
                mime_type=mime_type,
                fetch_url=full_res_url,
                full_res_url=full_res_url,
                upgraded=True,
            )
        except ingestion.IngestionError:
            pass

    image_bytes, mime_type = ingestion.fetch_image_bytes(image_url)
    return RemoteImageFetchResult(
        image_bytes=image_bytes,
        mime_type=mime_type,
        fetch_url=image_url,
        full_res_url=full_res_url,
        upgraded=False,
    )

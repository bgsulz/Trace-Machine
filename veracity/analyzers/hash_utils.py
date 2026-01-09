from __future__ import annotations

from typing import List, Tuple

import imagehash


def _safe_hex_to_hash(hex_value: str | None):
    if not hex_value:
        return None
    try:
        return imagehash.hex_to_hash(hex_value)
    except Exception:
        return None


def _hamming_distance(a, b) -> int | None:
    if a is None or b is None:
        return None
    try:
        return int(a - b)
    except Exception:
        return None


def compute_base_hashes(phash_hex: str | None, whash_hex: str | None):
    return _safe_hex_to_hash(phash_hex), _safe_hex_to_hash(whash_hex)


def compute_neighbor_distances(
    base_phash,
    base_whash,
    neighbor_phash_hex: str | None,
    neighbor_whash_hex: str | None,
) -> Tuple[int | None, int | None, str | None, str, int | None]:
    neighbor_phash = _safe_hex_to_hash(neighbor_phash_hex)
    neighbor_whash = _safe_hex_to_hash(neighbor_whash_hex)

    phash_distance = _hamming_distance(base_phash, neighbor_phash)
    whash_distance = _hamming_distance(base_whash, neighbor_whash)

    display_hash = neighbor_phash_hex
    display_label = "phash"
    display_distance = phash_distance if phash_distance is not None else 0

    if neighbor_whash_hex and whash_distance is not None and (
        phash_distance is None or whash_distance <= phash_distance
    ):
        display_hash = neighbor_whash_hex
        display_label = "whash"
        display_distance = whash_distance

    return phash_distance, whash_distance, display_hash, display_label, display_distance


def extract_sources(neighbor, limit: int = 3) -> List[dict[str, str]]:
    sources: List[dict[str, str]] = []
    for src in getattr(neighbor, "sources", [])[:limit]:
        url = getattr(src, "url", None)
        if url:
            sources.append({"url": url})
    return sources

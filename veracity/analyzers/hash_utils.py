from __future__ import annotations

from typing import List, Tuple

import imagehash

MATCH_METHOD_LABELS = {
    "hybrid": "Hybrid (hash + local)",
    "local": "Local geometric",
    "hash": "Hash",
}


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


def format_hash_display(
    display_hash: str | None,
    display_label: str,
    fallback_hash: str | None,
) -> str | None:
    if display_hash:
        return f"{display_hash} ({display_label})"
    return fallback_hash


def match_method_label(match_method: str | None) -> str:
    method = str(match_method or "hash").lower()
    return MATCH_METHOD_LABELS.get(method, "Hash")


def local_match_payload(
    local_snapshot,
    *,
    include_homography: bool = False,
) -> dict[str, object] | None:
    if local_snapshot is None:
        return None
    payload: dict[str, object] = {
        "extractor": getattr(local_snapshot, "extractor", "orb"),
        "good_matches": int(getattr(local_snapshot, "good_matches", 0) or 0),
        "inliers": int(getattr(local_snapshot, "inliers", 0) or 0),
        "inlier_ratio": float(getattr(local_snapshot, "inlier_ratio", 0.0) or 0.0),
        "crop_box": getattr(local_snapshot, "crop_box", None),
    }
    if include_homography:
        payload["homography_found"] = bool(
            getattr(local_snapshot, "homography_found", False)
        )
    return payload


def extract_sources(neighbor, limit: int = 3) -> List[dict[str, str]]:
    sources: List[dict[str, str]] = []
    for src in getattr(neighbor, "sources", [])[:limit]:
        url = getattr(src, "url", None)
        if url:
            sources.append({"url": url})
    return sources

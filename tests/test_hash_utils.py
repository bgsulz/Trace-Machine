import imagehash

from veracity.analyzers.hash_utils import (
    compute_base_hashes,
    compute_neighbor_distances,
    extract_sources,
)


def test_compute_base_hashes_returns_hash_objects():
    base_phash, base_whash = compute_base_hashes(
        "ffffffffffffffff", "0000000000000000"
    )

    assert isinstance(base_phash, imagehash.ImageHash)
    assert isinstance(base_whash, imagehash.ImageHash)
    assert str(base_phash) == "ffffffffffffffff"
    assert str(base_whash) == "0000000000000000"


def test_compute_neighbor_distances_prefers_whash_when_closer():
    base_phash, base_whash = compute_base_hashes(
        "ffffffffffffffff", "0000000000000000"
    )

    phash_hex = "ffffffff00000000"
    whash_hex = "0000000000000001"

    (
        phash_distance,
        whash_distance,
        display_hash,
        display_label,
        display_distance,
    ) = compute_neighbor_distances(base_phash, base_whash, phash_hex, whash_hex)

    assert phash_distance > whash_distance
    assert whash_distance == 1
    assert display_hash == whash_hex
    assert display_label == "whash"
    assert display_distance == whash_distance


def test_compute_neighbor_distances_handles_missing_hashes():
    base_phash, base_whash = compute_base_hashes(
        "ffffffffffffffff", "0000000000000000"
    )

    (
        phash_distance,
        whash_distance,
        display_hash,
        display_label,
        display_distance,
    ) = compute_neighbor_distances(base_phash, base_whash, None, None)

    assert phash_distance is None
    assert whash_distance is None
    assert display_hash is None
    assert display_label == "phash"
    assert display_distance == 0


def test_extract_sources_limits_results_and_skips_missing_urls():
    class Source:
        def __init__(self, url: str | None):
            self.url = url

    neighbor = type(
        "Neighbor",
        (),
        {
            "sources": [
                Source("https://example.com/a"),
                Source("https://example.com/b"),
                Source(None),
                Source("https://example.com/c"),
            ]
        },
    )

    sources = extract_sources(neighbor, limit=2)

    assert len(sources) == 2
    assert all(entry["url"].startswith("https://example.com/") for entry in sources)

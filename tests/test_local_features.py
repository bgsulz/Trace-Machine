import numpy as np
from types import SimpleNamespace

from veracity.matching.local_features import (
    _compute_inlier_coverage,
    _dedupe_candidate_matches,
    LocalFeaturePayload,
    LocalMatchTuning,
    deserialize_feature_payload,
    select_persistent_features,
    serialize_feature_payload,
)


def _payload(extractor: str, *, points: int = 3) -> LocalFeaturePayload:
    return LocalFeaturePayload(
        extractor=extractor,
        width=120,
        height=80,
        points=np.array([[float(i), float(i + 1)] for i in range(points)], dtype=np.float32),
        descriptors=np.zeros((points, 32), dtype=np.uint8),
    )


def test_feature_payload_roundtrip():
    original = _payload("orb", points=4)

    blob = serialize_feature_payload(original)
    decoded = deserialize_feature_payload(blob)

    assert decoded is not None
    assert decoded.extractor == original.extractor
    assert decoded.width == original.width
    assert decoded.height == original.height
    assert np.array_equal(decoded.points, original.points)
    assert np.array_equal(decoded.descriptors, original.descriptors)


def test_select_persistent_features_falls_back_to_akaze(monkeypatch):
    orb = _payload("orb", points=50)
    akaze = _payload("akaze", points=120)

    def _fake_extract(_image_bytes, extractor: str, *, tuning):
        if extractor == "orb":
            return orb
        if extractor == "akaze":
            return akaze
        return None

    monkeypatch.setattr(
        "veracity.matching.local_features.extract_features",
        _fake_extract,
    )
    tuning = LocalMatchTuning(orb_min_keypoints=100, enable_akaze_fallback=True)

    selected = select_persistent_features(b"image", tuning=tuning)

    assert selected is not None
    assert selected.extractor == "akaze"


def test_select_persistent_features_keeps_orb_when_fallback_disabled(monkeypatch):
    orb = _payload("orb", points=50)

    def _fake_extract(_image_bytes, extractor: str, *, tuning):
        if extractor == "orb":
            return orb
        return None

    monkeypatch.setattr(
        "veracity.matching.local_features.extract_features",
        _fake_extract,
    )
    tuning = LocalMatchTuning(orb_min_keypoints=100, enable_akaze_fallback=False)

    selected = select_persistent_features(b"image", tuning=tuning)

    assert selected is not None
    assert selected.extractor == "orb"


def test_dedupe_candidate_matches_keeps_best_per_train_index():
    matches = [
        SimpleNamespace(queryIdx=0, trainIdx=7, distance=21.0),
        SimpleNamespace(queryIdx=1, trainIdx=7, distance=11.0),
        SimpleNamespace(queryIdx=3, trainIdx=9, distance=19.0),
    ]

    deduped = _dedupe_candidate_matches(matches)

    assert len(deduped) == 2
    assert any(m.trainIdx == 7 and m.queryIdx == 1 for m in deduped)
    assert any(m.trainIdx == 9 and m.queryIdx == 3 for m in deduped)


def test_compute_inlier_coverage_is_small_for_tight_cluster():
    points = np.array(
        [
            [[10.0, 10.0]],
            [[11.0, 10.0]],
            [[11.0, 11.0]],
            [[10.0, 11.0]],
        ],
        dtype=np.float32,
    )
    mask = np.array([1, 1, 1, 1], dtype=np.uint8)

    coverage = _compute_inlier_coverage(points, mask, width=100, height=100)

    assert coverage < 0.001


def test_compute_inlier_coverage_is_large_for_wide_spread():
    points = np.array(
        [
            [[10.0, 10.0]],
            [[90.0, 10.0]],
            [[90.0, 90.0]],
            [[10.0, 90.0]],
        ],
        dtype=np.float32,
    )
    mask = np.array([1, 1, 1, 1], dtype=np.uint8)

    coverage = _compute_inlier_coverage(points, mask, width=100, height=100)

    assert coverage > 0.6

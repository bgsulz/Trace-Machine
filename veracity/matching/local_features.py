from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import logging

import numpy as np
from PIL import Image, ImageOps

try:  # pragma: no cover - import guard
    import cv2
except ImportError:  # pragma: no cover - handled at runtime
    cv2 = None


logger = logging.getLogger(__name__)

# Conservative defaults. Sensible ranges are documented to make tuning easy.
#
# 800-1800: lower is faster, higher can recover more keypoints on large images.
DEFAULT_MAX_IMAGE_SIDE = 1200
# 800-3000: lower is faster, higher can improve difficult matches.
DEFAULT_ORB_NFEATURES = 1500
# 80-250: lower captures more weak ORB results, higher prefers stronger ones.
DEFAULT_ORB_MIN_KEYPOINTS = 140
# 0.65-0.85: lower is stricter (fewer false positives, lower recall).
DEFAULT_LOWES_RATIO = 0.72
# 12-40: higher is stricter.
DEFAULT_MIN_GOOD_MATCHES = 24
# 10-35: higher is stricter.
DEFAULT_MIN_INLIERS = 16
# 0.25-0.70: higher is stricter.
DEFAULT_MIN_INLIER_RATIO = 0.45
# 2.0-5.0: lower is stricter geometric fit.
DEFAULT_RANSAC_REPROJ_THRESHOLD = 3.0
# 0.05-0.25: higher requires matches to explain a larger fraction of keypoints.
DEFAULT_MIN_INLIER_KEYPOINT_SUPPORT = 0.10
# 0.05-0.30: higher requires inliers to cover meaningful area in at least one image.
DEFAULT_MIN_PRIMARY_INLIER_COVERAGE = 0.12
# 0.00-0.08: higher rejects tiny clustered matches on both images.
DEFAULT_MIN_SECONDARY_INLIER_COVERAGE = 0.01

FEATURE_VERSION = 1
EXTRACTOR_ORB = "orb"
EXTRACTOR_AKAZE = "akaze"
SUPPORTED_EXTRACTORS = (EXTRACTOR_ORB, EXTRACTOR_AKAZE)


@dataclass(frozen=True, slots=True)
class LocalMatchTuning:
    max_image_side: int = DEFAULT_MAX_IMAGE_SIDE
    orb_nfeatures: int = DEFAULT_ORB_NFEATURES
    orb_min_keypoints: int = DEFAULT_ORB_MIN_KEYPOINTS
    lowes_ratio: float = DEFAULT_LOWES_RATIO
    min_good_matches: int = DEFAULT_MIN_GOOD_MATCHES
    min_inliers: int = DEFAULT_MIN_INLIERS
    min_inlier_ratio: float = DEFAULT_MIN_INLIER_RATIO
    ransac_reproj_threshold: float = DEFAULT_RANSAC_REPROJ_THRESHOLD
    min_inlier_keypoint_support: float = DEFAULT_MIN_INLIER_KEYPOINT_SUPPORT
    min_primary_inlier_coverage: float = DEFAULT_MIN_PRIMARY_INLIER_COVERAGE
    min_secondary_inlier_coverage: float = DEFAULT_MIN_SECONDARY_INLIER_COVERAGE
    enable_akaze_fallback: bool = True


@dataclass(frozen=True, slots=True)
class LocalFeaturePayload:
    extractor: str
    width: int
    height: int
    points: np.ndarray
    descriptors: np.ndarray
    version: int = FEATURE_VERSION

    @property
    def keypoint_count(self) -> int:
        return int(self.points.shape[0])


@dataclass(frozen=True, slots=True)
class LocalMatchEvidence:
    extractor: str
    passed: bool
    good_matches: int
    inliers: int
    inlier_ratio: float
    homography_found: bool
    normalized_box: tuple[float, float, float, float] | None
    reason: str
    query_inlier_coverage: float = 0.0
    candidate_inlier_coverage: float = 0.0
    inlier_keypoint_support: float = 0.0


def opencv_available() -> bool:
    return cv2 is not None


def select_persistent_features(
    image_bytes: bytes,
    *,
    tuning: LocalMatchTuning | None = None,
) -> LocalFeaturePayload | None:
    tuning = tuning or LocalMatchTuning()
    orb = extract_features(image_bytes, EXTRACTOR_ORB, tuning=tuning)
    if orb and orb.keypoint_count >= tuning.orb_min_keypoints:
        return orb

    if not tuning.enable_akaze_fallback:
        return orb

    akaze = extract_features(image_bytes, EXTRACTOR_AKAZE, tuning=tuning)
    if akaze and (orb is None or akaze.keypoint_count >= orb.keypoint_count):
        return akaze
    return orb or akaze


def extract_features(
    image_bytes: bytes,
    extractor: str,
    *,
    tuning: LocalMatchTuning | None = None,
) -> LocalFeaturePayload | None:
    if not opencv_available():
        return None

    tuning = tuning or LocalMatchTuning()
    extractor = extractor.strip().lower()
    if extractor not in SUPPORTED_EXTRACTORS:
        raise ValueError(f"Unsupported local extractor: {extractor}")

    gray = _load_grayscale_image(image_bytes, max_side=tuning.max_image_side)
    if gray is None:
        return None

    detector = _make_detector(extractor, tuning)
    keypoints, descriptors = detector.detectAndCompute(gray, None)
    if not keypoints or descriptors is None:
        return None

    points = np.array([kp.pt for kp in keypoints], dtype=np.float32)
    if points.ndim != 2 or points.shape[1] != 2:
        return None

    descriptors = np.asarray(descriptors)
    if descriptors.ndim != 2:
        return None
    if descriptors.dtype != np.uint8:
        # ORB/AKAZE binary descriptors should be uint8.
        descriptors = descriptors.astype(np.uint8)

    height, width = gray.shape[:2]
    return LocalFeaturePayload(
        extractor=extractor,
        width=int(width),
        height=int(height),
        points=points,
        descriptors=descriptors,
    )


def verify_local_match(
    query: LocalFeaturePayload,
    candidate: LocalFeaturePayload,
    *,
    tuning: LocalMatchTuning | None = None,
) -> LocalMatchEvidence:
    tuning = tuning or LocalMatchTuning()

    if not opencv_available():
        return LocalMatchEvidence(
            extractor=query.extractor,
            passed=False,
            good_matches=0,
            inliers=0,
            inlier_ratio=0.0,
            homography_found=False,
            normalized_box=None,
            reason="opencv_unavailable",
        )

    if query.extractor != candidate.extractor:
        return LocalMatchEvidence(
            extractor=query.extractor,
            passed=False,
            good_matches=0,
            inliers=0,
            inlier_ratio=0.0,
            homography_found=False,
            normalized_box=None,
            reason="extractor_mismatch",
        )

    if query.descriptors.size == 0 or candidate.descriptors.size == 0:
        return LocalMatchEvidence(
            extractor=query.extractor,
            passed=False,
            good_matches=0,
            inliers=0,
            inlier_ratio=0.0,
            homography_found=False,
            normalized_box=None,
            reason="empty_descriptors",
        )

    matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)
    knn = matcher.knnMatch(query.descriptors, candidate.descriptors, k=2)

    good_matches = []
    for pair in knn:
        if len(pair) < 2:
            continue
        first, second = pair
        if first.distance < tuning.lowes_ratio * second.distance:
            good_matches.append(first)
    good_matches = _dedupe_candidate_matches(good_matches)

    if len(good_matches) < tuning.min_good_matches:
        return LocalMatchEvidence(
            extractor=query.extractor,
            passed=False,
            good_matches=len(good_matches),
            inliers=0,
            inlier_ratio=0.0,
            homography_found=False,
            normalized_box=None,
            reason="too_few_good_matches",
        )

    if len(good_matches) < 4:
        return LocalMatchEvidence(
            extractor=query.extractor,
            passed=False,
            good_matches=len(good_matches),
            inliers=0,
            inlier_ratio=0.0,
            homography_found=False,
            normalized_box=None,
            reason="too_few_points_for_homography",
        )

    src = np.float32([query.points[m.queryIdx] for m in good_matches]).reshape(-1, 1, 2)
    dst = np.float32([candidate.points[m.trainIdx] for m in good_matches]).reshape(-1, 1, 2)

    homography, mask = cv2.findHomography(
        src,
        dst,
        cv2.RANSAC,
        tuning.ransac_reproj_threshold,
    )
    if homography is None or mask is None:
        return LocalMatchEvidence(
            extractor=query.extractor,
            passed=False,
            good_matches=len(good_matches),
            inliers=0,
            inlier_ratio=0.0,
            homography_found=False,
            normalized_box=None,
            reason="homography_not_found",
        )

    inliers = int(np.sum(mask.ravel()))
    inlier_ratio = inliers / float(len(good_matches)) if good_matches else 0.0
    inlier_mask = mask.ravel().astype(bool)
    query_inlier_coverage = _compute_inlier_coverage(
        src,
        inlier_mask,
        width=query.width,
        height=query.height,
    )
    candidate_inlier_coverage = _compute_inlier_coverage(
        dst,
        inlier_mask,
        width=candidate.width,
        height=candidate.height,
    )
    min_keypoints = min(query.keypoint_count, candidate.keypoint_count)
    inlier_keypoint_support = (
        inliers / float(min_keypoints) if min_keypoints > 0 else 0.0
    )
    normalized_box = _project_normalized_box(
        homography=homography,
        source_width=query.width,
        source_height=query.height,
        target_width=candidate.width,
        target_height=candidate.height,
    )

    primary_coverage = max(query_inlier_coverage, candidate_inlier_coverage)
    secondary_coverage = min(query_inlier_coverage, candidate_inlier_coverage)
    passed = (
        inliers >= tuning.min_inliers
        and inlier_ratio >= tuning.min_inlier_ratio
        and inlier_keypoint_support >= tuning.min_inlier_keypoint_support
        and primary_coverage >= tuning.min_primary_inlier_coverage
        and secondary_coverage >= tuning.min_secondary_inlier_coverage
        and normalized_box is not None
    )
    if passed:
        reason = "ok"
    elif inliers < tuning.min_inliers:
        reason = "insufficient_inliers"
    elif inlier_ratio < tuning.min_inlier_ratio:
        reason = "insufficient_inlier_ratio"
    elif inlier_keypoint_support < tuning.min_inlier_keypoint_support:
        reason = "insufficient_keypoint_support"
    elif primary_coverage < tuning.min_primary_inlier_coverage:
        reason = "insufficient_inlier_spread"
    elif secondary_coverage < tuning.min_secondary_inlier_coverage:
        reason = "insufficient_secondary_inlier_spread"
    elif normalized_box is None:
        reason = "invalid_projected_box"
    else:
        reason = "insufficient_geometric_consensus"
    return LocalMatchEvidence(
        extractor=query.extractor,
        passed=passed,
        good_matches=len(good_matches),
        inliers=inliers,
        inlier_ratio=float(inlier_ratio),
        homography_found=True,
        normalized_box=normalized_box,
        reason=reason,
        query_inlier_coverage=float(query_inlier_coverage),
        candidate_inlier_coverage=float(candidate_inlier_coverage),
        inlier_keypoint_support=float(inlier_keypoint_support),
    )


def serialize_feature_payload(features: LocalFeaturePayload) -> bytes:
    buffer = BytesIO()
    np.savez_compressed(
        buffer,
        version=np.array([int(features.version)], dtype=np.int16),
        extractor=np.array([features.extractor]),
        width=np.array([int(features.width)], dtype=np.int32),
        height=np.array([int(features.height)], dtype=np.int32),
        points=np.asarray(features.points, dtype=np.float32),
        descriptors=np.asarray(features.descriptors, dtype=np.uint8),
    )
    return buffer.getvalue()


def deserialize_feature_payload(payload: bytes) -> LocalFeaturePayload | None:
    if not payload:
        return None
    try:
        with np.load(BytesIO(payload), allow_pickle=False) as data:
            version = int(np.asarray(data["version"]).reshape(-1)[0])
            extractor = str(np.asarray(data["extractor"]).reshape(-1)[0]).strip().lower()
            width = int(np.asarray(data["width"]).reshape(-1)[0])
            height = int(np.asarray(data["height"]).reshape(-1)[0])
            points = np.asarray(data["points"], dtype=np.float32)
            descriptors = np.asarray(data["descriptors"], dtype=np.uint8)
    except Exception:
        logger.exception("Failed to deserialize local feature payload")
        return None

    if version != FEATURE_VERSION:
        return None
    if extractor not in SUPPORTED_EXTRACTORS:
        return None
    if points.ndim != 2 or points.shape[1] != 2:
        return None
    if descriptors.ndim != 2:
        return None
    if width <= 0 or height <= 0:
        return None

    return LocalFeaturePayload(
        extractor=extractor,
        width=width,
        height=height,
        points=points,
        descriptors=descriptors,
        version=version,
    )


def _load_grayscale_image(image_bytes: bytes, *, max_side: int) -> np.ndarray | None:
    try:
        with Image.open(BytesIO(image_bytes)) as img:
            img = ImageOps.exif_transpose(img).convert("L")
            width, height = img.size
            if width <= 0 or height <= 0:
                return None

            largest = max(width, height)
            if largest > max_side:
                scale = max_side / float(largest)
                new_size = (
                    max(1, int(round(width * scale))),
                    max(1, int(round(height * scale))),
                )
                resampling = getattr(Image, "Resampling", Image)
                img = img.resize(new_size, resampling.BILINEAR)

            return np.array(img, dtype=np.uint8)
    except Exception:
        logger.exception("Failed to decode image for local feature extraction")
        return None


def _make_detector(extractor: str, tuning: LocalMatchTuning):
    if extractor == EXTRACTOR_ORB:
        return cv2.ORB_create(nfeatures=int(tuning.orb_nfeatures))
    if extractor == EXTRACTOR_AKAZE:
        return cv2.AKAZE_create()
    raise ValueError(f"Unsupported local extractor: {extractor}")


def _dedupe_candidate_matches(matches):
    if not matches:
        return []

    best_by_train_idx = {}
    for match in matches:
        train_idx = int(getattr(match, "trainIdx", -1))
        if train_idx < 0:
            continue

        existing = best_by_train_idx.get(train_idx)
        if existing is None or float(match.distance) < float(existing.distance):
            best_by_train_idx[train_idx] = match

    deduped = list(best_by_train_idx.values())
    deduped.sort(
        key=lambda m: (
            int(getattr(m, "queryIdx", -1)),
            int(getattr(m, "trainIdx", -1)),
        )
    )
    return deduped


def _compute_inlier_coverage(
    points,
    inlier_mask,
    *,
    width: int,
    height: int,
) -> float:
    if width <= 0 or height <= 0:
        return 0.0

    flat_points = np.asarray(points, dtype=np.float32).reshape(-1, 2)
    mask = np.asarray(inlier_mask, dtype=bool).reshape(-1)
    if flat_points.shape[0] == 0 or flat_points.shape[0] != mask.shape[0]:
        return 0.0

    inlier_points = flat_points[mask]
    if inlier_points.shape[0] < 2:
        return 0.0

    min_x = float(np.min(inlier_points[:, 0]))
    max_x = float(np.max(inlier_points[:, 0]))
    min_y = float(np.min(inlier_points[:, 1]))
    max_y = float(np.max(inlier_points[:, 1]))

    span_x = max(0.0, min((max_x - min_x) / float(width), 1.0))
    span_y = max(0.0, min((max_y - min_y) / float(height), 1.0))
    return span_x * span_y


def _project_normalized_box(
    *,
    homography,
    source_width: int,
    source_height: int,
    target_width: int,
    target_height: int,
) -> tuple[float, float, float, float] | None:
    if target_width <= 0 or target_height <= 0:
        return None

    corners = np.float32(
        [
            [[0, 0]],
            [[source_width, 0]],
            [[source_width, source_height]],
            [[0, source_height]],
        ]
    )
    try:
        projected = cv2.perspectiveTransform(corners, homography).reshape(-1, 2)
    except Exception:
        return None

    min_x = float(np.min(projected[:, 0]))
    max_x = float(np.max(projected[:, 0]))
    min_y = float(np.min(projected[:, 1]))
    max_y = float(np.max(projected[:, 1]))

    left = max(0.0, min(min_x / target_width, 1.0))
    right = max(0.0, min(max_x / target_width, 1.0))
    top = max(0.0, min(min_y / target_height, 1.0))
    bottom = max(0.0, min(max_y / target_height, 1.0))

    width = right - left
    height = bottom - top
    if width <= 0.0 or height <= 0.0:
        return None

    return (left, top, width, height)

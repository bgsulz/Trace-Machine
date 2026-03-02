from __future__ import annotations

import logging
from dataclasses import dataclass
from io import BytesIO

from flask import current_app
import imagehash
from PIL import Image
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload, load_only

from . import db
from .analyzers.context import AnalysisContext
from .matching import (
    FEATURE_VERSION,
    LocalFeaturePayload,
    LocalMatchTuning,
    deserialize_feature_payload,
    extract_features,
    opencv_available,
    select_persistent_features,
    serialize_feature_payload,
    verify_local_match,
)
from .models import ImageLocalFeatures, ImageRegistry


logger = logging.getLogger(__name__)

_MAX_PHASH_DISTANCE = 4
_MAX_WHASH_DISTANCE = 6
_DEFAULT_LOCAL_MATCH_MAX_CANDIDATES = 200


@dataclass(slots=True)
class ConsensusSnapshot:
    vote_real: int
    vote_edited: int
    vote_ai: int


@dataclass(slots=True)
class SourceSnapshot:
    url: str


@dataclass(slots=True)
class FactSnapshot:
    analyzer: str
    data: str


@dataclass(slots=True)
class SynthIDSnapshot:
    detected: int
    not_detected: int


@dataclass(slots=True)
class LocalMatchSnapshot:
    extractor: str
    good_matches: int
    inliers: int
    inlier_ratio: float
    homography_found: bool
    crop_box: tuple[float, float, float, float] | None


@dataclass(slots=True)
class NeighborSnapshot:
    id: int | None
    phash: str | None
    whash: str | None
    created_at: object | None
    consensus: ConsensusSnapshot | None
    sources: tuple[SourceSnapshot, ...]
    facts: tuple[FactSnapshot, ...]
    synthid: SynthIDSnapshot | None
    match_method: str = "hash"
    local_match: LocalMatchSnapshot | None = None


def prepare_analysis_context(image_bytes: bytes) -> AnalysisContext:
    with Image.open(BytesIO(image_bytes)) as img:
        target_phash = imagehash.phash(img)
        target_whash = imagehash.whash(img)
        width, height = img.size
    phash_str = str(target_phash)
    whash_str = str(target_whash)

    registry_entry = _get_or_create_registry_entry(phash_str, whash_str)

    local_tuning = LocalMatchTuning()
    query_feature_cache: dict[str, LocalFeaturePayload] = {}
    local_matching_enabled = _local_matching_enabled()
    if local_matching_enabled:
        query_features = _load_or_create_registry_features(
            registry_entry, image_bytes, tuning=local_tuning
        )
        if query_features:
            query_feature_cache[query_features.extractor] = query_features

    base_phash = target_phash
    base_whash = target_whash

    candidate_images = _load_matching_candidates(local_matching_enabled)
    matched_order: list[int] = []
    match_context: dict[int, tuple[str, LocalMatchSnapshot | None]] = {}
    local_candidates_evaluated = 0
    local_candidate_budget = _local_match_max_candidates()

    for img in candidate_images:
        try:
            candidate_id = getattr(img, "id", None)
            if candidate_id is None:
                continue

            hash_matched = _is_hash_match(base_phash, base_whash, img)
            local_match = None
            local_matched = False

            if (
                local_matching_enabled
                and local_candidates_evaluated < local_candidate_budget
                and candidate_id != registry_entry.id
            ):
                local_match, attempted = _evaluate_local_candidate_match(
                    query_image_bytes=image_bytes,
                    query_feature_cache=query_feature_cache,
                    candidate=img,
                    tuning=local_tuning,
                )
                if attempted:
                    local_candidates_evaluated += 1
                local_matched = local_match is not None

            if (hash_matched or local_matched) and candidate_id not in match_context:
                matched_order.append(candidate_id)
                match_context[candidate_id] = (
                    _resolve_match_method(hash_matched, local_matched),
                    local_match,
                )
        except Exception:
            logger.exception(
                "Failed to evaluate registry neighbor candidate id=%s",
                getattr(img, "id", None),
            )
            continue

    neighbor_details = _load_neighbor_details(matched_order)
    neighbors: list[NeighborSnapshot] = []
    for image_id in matched_order:
        row = neighbor_details.get(image_id)
        if row is None:
            continue
        match_method, local_match = match_context[image_id]
        neighbors.append(
            _serialize_neighbor(
                row,
                match_method=match_method,
                local_match=local_match,
            )
        )

    return AnalysisContext(
        image_bytes=image_bytes,
        phash=phash_str,
        whash=whash_str,
        registry_id=registry_entry.id,
        neighbors=neighbors,
        width=width,
        height=height,
    )


def _get_or_create_registry_entry(phash: str, whash: str) -> ImageRegistry:
    registry_entry = ImageRegistry.query.filter_by(phash=phash).first()
    if registry_entry is not None:
        return registry_entry

    registry_entry = ImageRegistry(phash=phash, whash=whash)
    db.session.add(registry_entry)
    try:
        db.session.commit()
        return registry_entry
    except IntegrityError:
        # Another request created this hash concurrently.
        db.session.rollback()
        existing = ImageRegistry.query.filter_by(phash=phash).first()
        if existing is not None:
            return existing
        raise


def _load_matching_candidates(local_matching_enabled: bool) -> list[ImageRegistry]:
    options = [
        load_only(
            ImageRegistry.id,
            ImageRegistry.phash,
            ImageRegistry.whash,
        )
    ]
    if local_matching_enabled:
        options.append(joinedload(ImageRegistry.local_features))
    return ImageRegistry.query.options(*options).all()


def _load_neighbor_details(image_ids: list[int]) -> dict[int, ImageRegistry]:
    if not image_ids:
        return {}
    rows = (
        ImageRegistry.query.options(
            joinedload(ImageRegistry.consensus),
            joinedload(ImageRegistry.sources),
            joinedload(ImageRegistry.facts),
            joinedload(ImageRegistry.synthid_reports),
        )
        .filter(ImageRegistry.id.in_(image_ids))
        .all()
    )
    return {row.id: row for row in rows}


def _serialize_neighbor(
    registry_obj: ImageRegistry,
    *,
    match_method: str = "hash",
    local_match: LocalMatchSnapshot | None = None,
) -> NeighborSnapshot:
    consensus = getattr(registry_obj, "consensus", None)
    consensus_snapshot = None
    if consensus is not None:
        consensus_snapshot = ConsensusSnapshot(
            vote_real=int(consensus.vote_real or 0),
            vote_edited=int(consensus.vote_edited or 0),
            vote_ai=int(consensus.vote_ai or 0),
        )

    sources_snapshot: list[SourceSnapshot] = []
    for source in getattr(registry_obj, "sources", []) or []:
        url = getattr(source, "url", None)
        if url:
            sources_snapshot.append(SourceSnapshot(url=url))

    facts_snapshot: list[FactSnapshot] = []
    for fact in getattr(registry_obj, "facts", []) or []:
        analyzer = getattr(fact, "analyzer", None)
        data = getattr(fact, "data", None)
        if analyzer is None or data is None:
            continue
        facts_snapshot.append(FactSnapshot(analyzer=analyzer, data=data))

    synthid_snapshot = None
    synthid_reports = getattr(registry_obj, "synthid_reports", None) or []
    if synthid_reports:
        detected = sum(1 for r in synthid_reports if r.result == "detected")
        not_detected = sum(1 for r in synthid_reports if r.result == "not_detected")
        synthid_snapshot = SynthIDSnapshot(detected=detected, not_detected=not_detected)

    return NeighborSnapshot(
        id=getattr(registry_obj, "id", None),
        phash=getattr(registry_obj, "phash", None),
        whash=getattr(registry_obj, "whash", None),
        created_at=getattr(registry_obj, "created_at", None),
        consensus=consensus_snapshot,
        sources=tuple(sources_snapshot),
        facts=tuple(facts_snapshot),
        synthid=synthid_snapshot,
        match_method=match_method,
        local_match=local_match,
    )


def _is_hash_match(base_phash, base_whash, candidate: ImageRegistry) -> bool:
    candidate_phash = getattr(candidate, "phash", None)
    if not candidate_phash:
        return False

    try:
        h2_phash = imagehash.hex_to_hash(candidate_phash)
    except Exception:
        return False

    if (base_phash - h2_phash) <= _MAX_PHASH_DISTANCE:
        return True

    candidate_whash = getattr(candidate, "whash", None)
    if not candidate_whash:
        return False

    try:
        h2_whash = imagehash.hex_to_hash(candidate_whash)
    except Exception:
        return False
    return (base_whash - h2_whash) <= _MAX_WHASH_DISTANCE


def _evaluate_local_candidate_match(
    *,
    query_image_bytes: bytes,
    query_feature_cache: dict[str, LocalFeaturePayload],
    candidate: ImageRegistry,
    tuning: LocalMatchTuning,
) -> tuple[LocalMatchSnapshot | None, bool]:
    candidate_row = getattr(candidate, "local_features", None)
    if candidate_row is None or not getattr(candidate_row, "payload", None):
        return None, False

    candidate_features = deserialize_feature_payload(candidate_row.payload)
    if candidate_features is None:
        return None, False

    extractor = candidate_features.extractor
    query_features = query_feature_cache.get(extractor)
    if query_features is None:
        query_features = extract_features(
            query_image_bytes,
            extractor,
            tuning=tuning,
        )
        if query_features is not None:
            query_feature_cache[extractor] = query_features

    if query_features is None:
        return None, True

    evidence = verify_local_match(
        query_features,
        candidate_features,
        tuning=tuning,
    )
    if not evidence.passed:
        return None, True

    crop_box = None
    if evidence.normalized_box is not None:
        crop_box = tuple(float(v) for v in evidence.normalized_box)

    return (
        LocalMatchSnapshot(
            extractor=evidence.extractor,
            good_matches=evidence.good_matches,
            inliers=evidence.inliers,
            inlier_ratio=round(float(evidence.inlier_ratio), 4),
            homography_found=evidence.homography_found,
            crop_box=crop_box,
        ),
        True,
    )


def _resolve_match_method(hash_matched: bool, local_matched: bool) -> str:
    if hash_matched and local_matched:
        return "hybrid"
    if local_matched:
        return "local"
    return "hash"


def _local_matching_enabled() -> bool:
    if not opencv_available():
        return False
    return bool(current_app.config.get("LOCAL_MATCHING_ENABLED", True))


def _local_match_max_candidates() -> int:
    raw = current_app.config.get(
        "LOCAL_MATCH_MAX_CANDIDATES",
        _DEFAULT_LOCAL_MATCH_MAX_CANDIDATES,
    )
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_LOCAL_MATCH_MAX_CANDIDATES
    return max(1, value)


def _load_or_create_registry_features(
    registry_entry: ImageRegistry,
    image_bytes: bytes,
    *,
    tuning: LocalMatchTuning,
) -> LocalFeaturePayload | None:
    row = getattr(registry_entry, "local_features", None)
    if row is not None and int(getattr(row, "feature_version", 0) or 0) == FEATURE_VERSION:
        decoded = deserialize_feature_payload(getattr(row, "payload", b""))
        if decoded is not None:
            return decoded

    selected = select_persistent_features(image_bytes, tuning=tuning)
    if selected is None:
        return None

    serialized = serialize_feature_payload(selected)
    if row is None:
        row = ImageLocalFeatures(
            image_id=registry_entry.id,
            extractor=selected.extractor,
            feature_version=FEATURE_VERSION,
            keypoint_count=selected.keypoint_count,
            payload=serialized,
        )
        db.session.add(row)
    else:
        row.extractor = selected.extractor
        row.feature_version = FEATURE_VERSION
        row.keypoint_count = selected.keypoint_count
        row.payload = serialized

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception(
            "Failed to persist local feature payload for image_id=%s",
            registry_entry.id,
        )
        return None

    return selected

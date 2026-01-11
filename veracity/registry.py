import logging
from dataclasses import dataclass
from io import BytesIO

import imagehash
from PIL import Image
from sqlalchemy.orm import joinedload

from . import db
from .analyzers.context import AnalysisContext
from .models import ImageRegistry


logger = logging.getLogger(__name__)


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
class TinEyeSnapshot:
    total_matches: int
    earliest_date: object | None
    on_shame_list: bool


@dataclass(slots=True)
class NeighborSnapshot:
    id: int | None
    phash: str | None
    whash: str | None
    created_at: object | None
    consensus: ConsensusSnapshot | None
    sources: tuple[SourceSnapshot, ...]
    facts: tuple[FactSnapshot, ...]
    tineye_result: TinEyeSnapshot | None = None


def prepare_analysis_context(image_bytes: bytes) -> AnalysisContext:
    with Image.open(BytesIO(image_bytes)) as img:
        target_phash = imagehash.phash(img)
        target_whash = imagehash.whash(img)
        width, height = img.size
    phash_str = str(target_phash)
    whash_str = str(target_whash)

    registry_entry = ImageRegistry.query.filter_by(phash=phash_str).first()
    if not registry_entry:
        registry_entry = ImageRegistry(phash=phash_str, whash=whash_str)
        db.session.add(registry_entry)
        db.session.commit()

    base_phash = target_phash
    base_whash = target_whash

    all_images = ImageRegistry.query.options(
        joinedload(ImageRegistry.consensus),
        joinedload(ImageRegistry.sources),
        joinedload(ImageRegistry.facts),
        joinedload(ImageRegistry.tineye_result),
    ).all()

    neighbors = []
    seen_ids: set[int] = set()

    for img in all_images:
        try:
            matched = False
            h2_phash = imagehash.hex_to_hash(img.phash)
            if (base_phash - h2_phash) <= 4:
                matched = True

            img_whash_val = getattr(img, "whash", None)
            if img_whash_val:
                h2_whash = imagehash.hex_to_hash(img_whash_val)
                if (base_whash - h2_whash) <= 6:
                    matched = True

            if matched and img.id not in seen_ids:
                neighbors.append(_serialize_neighbor(img))
                seen_ids.add(img.id)
        except Exception:
            logger.exception(
                "Failed to evaluate registry neighbor candidate id=%s",
                getattr(img, "id", None),
            )
            continue

    return AnalysisContext(
        image_bytes=image_bytes,
        phash=phash_str,
        whash=whash_str,
        registry_id=registry_entry.id,
        neighbors=neighbors,
        width=width,
        height=height,
    )


def _serialize_neighbor(registry_obj: ImageRegistry) -> NeighborSnapshot:
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

    tineye_snapshot = None
    tineye_result = getattr(registry_obj, "tineye_result", None)
    if tineye_result is not None:
        tineye_snapshot = TinEyeSnapshot(
            total_matches=tineye_result.total_matches,
            earliest_date=tineye_result.earliest_date,
            on_shame_list=tineye_result.on_shame_list,
        )

    return NeighborSnapshot(
        id=getattr(registry_obj, "id", None),
        phash=getattr(registry_obj, "phash", None),
        whash=getattr(registry_obj, "whash", None),
        created_at=getattr(registry_obj, "created_at", None),
        consensus=consensus_snapshot,
        sources=tuple(sources_snapshot),
        facts=tuple(facts_snapshot),
        tineye_result=tineye_snapshot,
    )

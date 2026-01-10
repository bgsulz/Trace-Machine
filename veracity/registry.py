import imagehash
from io import BytesIO
from types import SimpleNamespace
from PIL import Image
from sqlalchemy.orm import joinedload
from . import db
from .models import ImageRegistry
from .analyzers.context import AnalysisContext


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
    elif not getattr(registry_entry, "whash", None):
        registry_entry.whash = whash_str
        db.session.add(registry_entry)
        db.session.commit()

    base_phash = imagehash.hex_to_hash(phash_str)
    base_whash = imagehash.hex_to_hash(whash_str)

    all_images = (
        ImageRegistry.query.options(
            joinedload(ImageRegistry.consensus),
            joinedload(ImageRegistry.sources),
            joinedload(ImageRegistry.facts),
        ).all()
    )

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


def _serialize_neighbor(registry_obj: ImageRegistry) -> SimpleNamespace:
    consensus = getattr(registry_obj, "consensus", None)
    consensus_snapshot = None
    if consensus is not None:
        consensus_snapshot = SimpleNamespace(
            vote_real=int(consensus.vote_real or 0),
            vote_edited=int(consensus.vote_edited or 0),
            vote_ai=int(consensus.vote_ai or 0),
        )

    sources_snapshot = []
    for source in getattr(registry_obj, "sources", []) or []:
        url = getattr(source, "url", None)
        if url:
            sources_snapshot.append(SimpleNamespace(url=url))

    facts_snapshot = []
    for fact in getattr(registry_obj, "facts", []) or []:
        analyzer = getattr(fact, "analyzer", None)
        data = getattr(fact, "data", None)
        if analyzer is None or data is None:
            continue
        facts_snapshot.append(SimpleNamespace(analyzer=analyzer, data=data))

    return SimpleNamespace(
        id=registry_obj.id,
        phash=getattr(registry_obj, "phash", None),
        whash=getattr(registry_obj, "whash", None),
        created_at=getattr(registry_obj, "created_at", None),
        consensus=consensus_snapshot,
        sources=sources_snapshot,
        facts=facts_snapshot,
    )

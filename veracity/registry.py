import imagehash
from io import BytesIO
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

    all_images = ImageRegistry.query.options(
        joinedload(ImageRegistry.consensus),
        joinedload(ImageRegistry.sources),
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
                neighbors.append(img)
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

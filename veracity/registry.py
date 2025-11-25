import imagehash
from io import BytesIO
from PIL import Image
from sqlalchemy.orm import joinedload
from . import db
from .models import ImageRegistry
from .analyzers.context import AnalysisContext


def prepare_analysis_context(image_bytes: bytes) -> AnalysisContext:
    with Image.open(BytesIO(image_bytes)) as img:
        target_hash = imagehash.phash(img)
    phash_str = str(target_hash)

    registry_entry = ImageRegistry.query.filter_by(phash=phash_str).first()
    if not registry_entry:
        registry_entry = ImageRegistry(phash=phash_str)
        db.session.add(registry_entry)
        db.session.commit()

    base_hash = imagehash.hex_to_hash(phash_str)

    all_images = ImageRegistry.query.options(
        joinedload(ImageRegistry.consensus),
        joinedload(ImageRegistry.sources),
    ).all()

    neighbors = []

    for img in all_images:
        try:
            h2 = imagehash.hex_to_hash(img.phash)
            if (base_hash - h2) <= 4:
                neighbors.append(img)
        except Exception:
            continue

    return AnalysisContext(
        image_bytes=image_bytes,
        phash=phash_str,
        registry_id=registry_entry.id,
        neighbors=neighbors,
    )

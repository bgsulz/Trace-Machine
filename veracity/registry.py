import imagehash
from io import BytesIO
from PIL import Image
from . import db
from .models import ImageRegistry

def prepare_analysis_context(image_bytes: bytes):
    with Image.open(BytesIO(image_bytes)) as img:
        target_hash = imagehash.phash(img)
    phash_str = str(target_hash)

    registry_entry = ImageRegistry.query.filter_by(phash=phash_str).first()
    if not registry_entry:
        registry_entry = ImageRegistry(phash=phash_str)
        db.session.add(registry_entry)
        db.session.commit()
    
    all_images = ImageRegistry.query.all()
    neighbors = []
    
    for img in all_images:
        try:
            h1 = imagehash.hex_to_hash(phash_str)
            h2 = imagehash.hex_to_hash(img.phash)
            if (h1 - h2) <= 4: 
                neighbors.append(img)
        except Exception:
            continue
            
    from .analyzers.manager import AnalysisContext
    return AnalysisContext(
        image_bytes=image_bytes,
        phash=phash_str,
        registry_id=registry_entry.id,
        neighbors=neighbors
    )
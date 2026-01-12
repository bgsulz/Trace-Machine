import io

import imagehash
import pytest
from PIL import Image

from veracity import db
from veracity.models import ImageRegistry
from veracity.registry import prepare_analysis_context

from conftest import _make_test_image_bytes


@pytest.fixture(autouse=True)
def _push_app_context(app):
    with app.app_context():
        yield


def _make_image_with_hash():
    """Create test image and return (bytes, phash_str, whash_str)."""
    image_bytes = _make_test_image_bytes()
    with Image.open(io.BytesIO(image_bytes)) as img:
        phash = imagehash.phash(img)
        whash = imagehash.whash(img)
    return image_bytes, str(phash), str(whash)


def _flip_bits(hash_str: str, num_bits: int) -> str:
    """Flip the first num_bits of a hex hash string."""
    hash_obj = imagehash.hex_to_hash(hash_str)
    arr = hash_obj.hash.copy()
    flipped = 0
    for i in range(arr.shape[0]):
        for j in range(arr.shape[1]):
            if flipped >= num_bits:
                break
            arr[i, j] = not arr[i, j]
            flipped += 1
        if flipped >= num_bits:
            break
    return str(imagehash.ImageHash(arr))


class TestPrepareAnalysisContext:
    def test_creates_registry_entry_if_not_exists(self):
        image_bytes, phash, whash = _make_image_with_hash()

        assert ImageRegistry.query.filter_by(phash=phash).first() is None

        context = prepare_analysis_context(image_bytes)

        assert context.phash == phash
        assert context.registry_id is not None
        assert ImageRegistry.query.filter_by(phash=phash).first() is not None

    def test_returns_existing_registry_entry(self):
        image_bytes, phash, whash = _make_image_with_hash()

        existing = ImageRegistry(phash=phash, whash=whash)
        db.session.add(existing)
        db.session.commit()
        existing_id = existing.id

        context = prepare_analysis_context(image_bytes)

        assert context.registry_id == existing_id

    def test_includes_neighbor_with_close_phash(self):
        image_bytes, phash, whash = _make_image_with_hash()

        close_phash = _flip_bits(phash, 3)
        neighbor = ImageRegistry(phash=close_phash, whash="0000000000000000")
        db.session.add(neighbor)
        db.session.commit()

        context = prepare_analysis_context(image_bytes)

        neighbor_phashes = [n.phash for n in context.neighbors]
        assert close_phash in neighbor_phashes

    def test_includes_neighbor_with_close_whash(self):
        image_bytes, phash, whash = _make_image_with_hash()

        close_whash = _flip_bits(whash, 5)
        far_phash = _flip_bits(phash, 10)
        neighbor = ImageRegistry(phash=far_phash, whash=close_whash)
        db.session.add(neighbor)
        db.session.commit()

        context = prepare_analysis_context(image_bytes)

        neighbor_whashes = [n.whash for n in context.neighbors]
        assert close_whash in neighbor_whashes

    def test_excludes_neighbor_with_distant_hashes(self):
        image_bytes, phash, whash = _make_image_with_hash()

        far_phash = _flip_bits(phash, 10)
        far_whash = _flip_bits(whash, 12)
        neighbor = ImageRegistry(phash=far_phash, whash=far_whash)
        db.session.add(neighbor)
        db.session.commit()

        context = prepare_analysis_context(image_bytes)

        neighbor_phashes = [n.phash for n in context.neighbors]
        assert far_phash not in neighbor_phashes

    def test_handles_invalid_hash_in_registry_gracefully(self):
        image_bytes, phash, whash = _make_image_with_hash()

        bad_neighbor = ImageRegistry(phash="not-a-valid-hash", whash="also-invalid")
        db.session.add(bad_neighbor)
        db.session.commit()

        context = prepare_analysis_context(image_bytes)

        assert context.registry_id is not None

    def test_includes_image_dimensions(self):
        image_bytes = _make_test_image_bytes(size=(100, 50))

        context = prepare_analysis_context(image_bytes)

        assert context.width == 100
        assert context.height == 50

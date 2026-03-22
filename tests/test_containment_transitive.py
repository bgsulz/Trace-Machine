"""Tests for transitive containment links and depth cap."""
import json
import uuid

import pytest

from veracity import db
from veracity.models import ImageContainment, ImageRegistry
from veracity.services.containment_service import (
    _MAX_CONTAINMENT_DEPTH,
    _compose_crop_boxes,
    save_containment_link,
)


def _make_registry():
    """Create a bare ImageRegistry row and return its id.  Must be called
    inside an app context."""
    entry = ImageRegistry(phash=f"ph_{uuid.uuid4().hex}", whash="wh")
    db.session.add(entry)
    db.session.commit()
    return entry.id


def _all_links():
    return [
        (r.parent_id, r.child_id, json.loads(r.crop_box_json))
        for r in ImageContainment.query.order_by(ImageContainment.id).all()
    ]


class TestComposeBoxes:
    def test_identity(self):
        """Cropping the full area of the parent returns the parent box."""
        outer = [0.1, 0.2, 0.5, 0.4]
        inner = [0.0, 0.0, 1.0, 1.0]
        result = _compose_crop_boxes(outer, inner)
        assert result == pytest.approx(outer)

    def test_half_of_half(self):
        """Taking the left-half of a left-half yields a quarter-width box."""
        outer = [0.0, 0.0, 0.5, 1.0]
        inner = [0.0, 0.0, 0.5, 1.0]
        result = _compose_crop_boxes(outer, inner)
        assert result == pytest.approx([0.0, 0.0, 0.25, 1.0])

    def test_offset_composition(self):
        """Inner offset is scaled by outer dimensions and added to outer origin."""
        outer = [0.2, 0.3, 0.4, 0.5]
        inner = [0.5, 0.5, 0.5, 0.5]
        result = _compose_crop_boxes(outer, inner)
        assert result == pytest.approx([0.4, 0.55, 0.2, 0.25])


class TestTransitiveContainment:
    def test_two_level_chain(self, app):
        """Cropping a crop creates a transitive link to the grandparent."""
        with app.app_context():
            a = _make_registry()
            b = _make_registry()
            c = _make_registry()

            save_containment_link(a, b, [0.0, 0.0, 0.5, 0.5])
            save_containment_link(b, c, [0.0, 0.0, 0.5, 0.5])

            links = _all_links()
            parent_child_pairs = [(p, ch) for p, ch, _ in links]
            assert (a, b) in parent_child_pairs
            assert (b, c) in parent_child_pairs
            assert (a, c) in parent_child_pairs  # transitive

            # Composed box: half of half = quarter
            transitive = next(box for p, ch, box in links if p == a and ch == c)
            assert transitive == pytest.approx([0.0, 0.0, 0.25, 0.25])

    def test_three_level_chain(self, app):
        """A→B→C→D creates transitive links A→C, A→D, and B→D."""
        with app.app_context():
            a = _make_registry()
            b = _make_registry()
            c = _make_registry()
            d = _make_registry()

            save_containment_link(a, b, [0.0, 0.0, 0.5, 1.0])
            save_containment_link(b, c, [0.0, 0.0, 0.5, 1.0])
            save_containment_link(c, d, [0.0, 0.0, 0.5, 1.0])

            links = _all_links()
            parent_child_pairs = [(p, ch) for p, ch, _ in links]
            assert (a, d) in parent_child_pairs
            assert (b, d) in parent_child_pairs

    def test_depth_cap(self, app):
        """Chains deeper than _MAX_CONTAINMENT_DEPTH do not propagate further."""
        with app.app_context():
            ids = [_make_registry() for _ in range(_MAX_CONTAINMENT_DEPTH + 2)]

            for i in range(len(ids) - 1):
                save_containment_link(ids[i], ids[i + 1], [0.0, 0.0, 0.8, 0.8])

            links = _all_links()
            deepest_child = ids[-1]

            # The deepest child should NOT be linked all the way back to ids[0]
            # because the chain exceeds _MAX_CONTAINMENT_DEPTH.
            ancestors_of_deepest = [p for p, ch, _ in links if ch == deepest_child]
            assert ids[0] not in ancestors_of_deepest

    def test_no_duplicate_transitive_links(self, app):
        """Calling save_containment_link twice doesn't create duplicate rows."""
        with app.app_context():
            a = _make_registry()
            b = _make_registry()
            c = _make_registry()

            save_containment_link(a, b, [0.0, 0.0, 0.5, 0.5])
            save_containment_link(b, c, [0.0, 0.0, 0.5, 0.5])
            # Call again — should be idempotent
            save_containment_link(b, c, [0.0, 0.0, 0.5, 0.5])

            links = _all_links()
            assert len(links) == 3  # A→B, B→C, A→C — no duplicates

import json

import pytest

from veracity import db
from veracity.models import ImageRegistry, ImageConsensus, ImageContainment, ProvenanceFact
from veracity.containment_service import save_containment_link, get_displayable_containments


@pytest.fixture(autouse=True)
def _push_app_context(app):
    with app.app_context():
        yield


def _create_registry(phash: str) -> ImageRegistry:
    registry = ImageRegistry(phash=phash, whash=phash)
    db.session.add(registry)
    db.session.commit()
    return registry


class TestSaveContainmentLink:
    def test_creates_link(self):
        parent = _create_registry("parent1")
        child = _create_registry("child1")

        result = save_containment_link(parent.id, child.id, [0.0, 0.0, 0.5, 0.5])

        assert result is not None
        assert result.parent_id == parent.id
        assert result.child_id == child.id

    def test_same_parent_and_child_returns_none(self):
        registry = _create_registry("selfref")

        result = save_containment_link(registry.id, registry.id, [0.0, 0.0, 1.0, 1.0])

        assert result is None

    def test_invalid_crop_box_length_raises(self):
        parent = _create_registry("parent2")
        child = _create_registry("child2")

        with pytest.raises(ValueError, match="four normalized values"):
            save_containment_link(parent.id, child.id, [0.0, 0.0, 0.5])

    def test_duplicate_link_returns_existing(self):
        parent = _create_registry("parent3")
        child = _create_registry("child3")
        crop_box = [0.1, 0.1, 0.8, 0.8]

        first = save_containment_link(parent.id, child.id, crop_box)
        second = save_containment_link(parent.id, child.id, crop_box)

        assert first.id == second.id
        assert ImageContainment.query.count() == 1

    def test_normalizes_crop_box_values(self):
        parent = _create_registry("parent4")
        child = _create_registry("child4")

        result = save_containment_link(parent.id, child.id, [-0.1, 1.5, 0.5, 0.5])

        crop_box = json.loads(result.crop_box_json)
        assert crop_box[0] == 0.0
        assert crop_box[1] == 1.0


class TestGetDisplayableContainments:
    def test_returns_empty_for_no_containments(self):
        parent = _create_registry("nochildren")

        result = get_displayable_containments(parent.id)

        assert result == []

    def test_excludes_children_with_no_votes_and_no_facts(self):
        parent = _create_registry("parent5")
        child = _create_registry("child5")

        containment = ImageContainment(
            parent_id=parent.id,
            child_id=child.id,
            crop_box_json=json.dumps([0.0, 0.0, 0.5, 0.5]),
        )
        db.session.add(containment)
        db.session.commit()

        result = get_displayable_containments(parent.id)

        assert result == []

    def test_includes_children_with_votes(self):
        parent = _create_registry("parent6")
        child = _create_registry("child6")

        consensus = ImageConsensus(image_id=child.id, vote_real=1)
        db.session.add(consensus)

        containment = ImageContainment(
            parent_id=parent.id,
            child_id=child.id,
            crop_box_json=json.dumps([0.0, 0.0, 0.5, 0.5]),
        )
        db.session.add(containment)
        db.session.commit()

        result = get_displayable_containments(parent.id)

        assert len(result) == 1
        assert result[0]["vote_real"] == 1

    def test_includes_children_with_facts(self):
        parent = _create_registry("parent7")
        child = _create_registry("child7")

        fact = ProvenanceFact(image_id=child.id, analyzer="c2pa", data="Signed")
        db.session.add(fact)

        containment = ImageContainment(
            parent_id=parent.id,
            child_id=child.id,
            crop_box_json=json.dumps([0.0, 0.0, 0.5, 0.5]),
        )
        db.session.add(containment)
        db.session.commit()

        result = get_displayable_containments(parent.id)

        assert len(result) == 1
        assert result[0]["fact_count"] == 1

    def test_parses_crop_box_correctly(self):
        parent = _create_registry("parent8")
        child = _create_registry("child8")

        consensus = ImageConsensus(image_id=child.id, vote_ai=2)
        db.session.add(consensus)

        containment = ImageContainment(
            parent_id=parent.id,
            child_id=child.id,
            crop_box_json=json.dumps([0.1, 0.2, 0.3, 0.4]),
        )
        db.session.add(containment)
        db.session.commit()

        result = get_displayable_containments(parent.id)

        assert result[0]["crop_box"] == [0.1, 0.2, 0.3, 0.4]

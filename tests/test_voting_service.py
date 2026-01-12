import pytest

from veracity import db
from veracity.models import ImageRegistry, ImageConsensus, ImageSource
from veracity.voting_service import (
    apply_vote,
    persist_source_url,
    get_or_create_consensus,
    build_voter_id,
    _increment_vote_counts,
    _decrement_vote_counts,
)


def _create_registry(phash: str) -> ImageRegistry:
    """Helper to create a registry with required whash field."""
    registry = ImageRegistry(phash=phash, whash=phash)
    db.session.add(registry)
    db.session.commit()
    return registry


@pytest.fixture(autouse=True)
def _push_app_context(app):
    with app.app_context():
        yield


class TestApplyVote:
    def test_invalid_vote_kind_returns_false(self):
        success, status = apply_vote("somehash", "invalid_vote", "voter123")
        assert success is False
        assert status is None

    def test_valid_vote_on_existing_registry(self):
        registry = _create_registry("testhash1")

        success, status = apply_vote("testhash1", "real", "voter1")
        assert success is True
        assert status == "recorded"

        consensus = ImageConsensus.query.filter_by(image_id=registry.id).first()
        assert consensus is not None
        assert consensus.vote_real == 1

    def test_same_voter_same_vote_is_unchanged(self):
        _create_registry("testhash2")

        apply_vote("testhash2", "ai", "voter2")
        success, status = apply_vote("testhash2", "ai", "voter2")

        assert success is True
        assert status == "unchanged"

    def test_same_voter_different_vote_updates(self):
        registry = _create_registry("testhash3")

        apply_vote("testhash3", "real", "voter3")
        success, status = apply_vote("testhash3", "ai", "voter3")

        assert success is True
        assert status == "updated"

        consensus = ImageConsensus.query.filter_by(image_id=registry.id).first()
        assert consensus.vote_real == 0
        assert consensus.vote_ai == 1


class TestDecrementVoteCounts:
    def test_decrement_does_not_go_negative(self):
        registry = _create_registry("dectest")

        consensus = ImageConsensus(image_id=registry.id, vote_real=0, vote_ai=0, vote_edited=0)
        db.session.add(consensus)
        db.session.commit()

        _decrement_vote_counts(consensus, "real")
        _decrement_vote_counts(consensus, "ai")
        _decrement_vote_counts(consensus, "edited")

        assert consensus.vote_real == 0
        assert consensus.vote_ai == 0
        assert consensus.vote_edited == 0


class TestIncrementVoteCounts:
    def test_increment_all_types(self):
        registry = _create_registry("inctest")

        consensus = ImageConsensus(image_id=registry.id)
        db.session.add(consensus)

        _increment_vote_counts(consensus, "real")
        _increment_vote_counts(consensus, "edited")
        _increment_vote_counts(consensus, "ai")
        _increment_vote_counts(consensus, "ai")

        assert consensus.vote_real == 1
        assert consensus.vote_edited == 1
        assert consensus.vote_ai == 2


class TestPersistSourceUrl:
    def test_creates_source_record(self):
        _create_registry("sourcehash1")

        persist_source_url("sourcehash1", "https://example.com/image.png")

        registry = ImageRegistry.query.filter_by(phash="sourcehash1").first()
        source = ImageSource.query.filter_by(image_id=registry.id).first()
        assert source is not None
        assert source.url == "https://example.com/image.png"

    def test_skips_when_phash_is_none(self):
        persist_source_url(None, "https://example.com/image.png")
        assert ImageSource.query.count() == 0

    def test_skips_when_url_is_none(self):
        persist_source_url("somehash", None)
        source = ImageSource.query.filter_by(url=None).first()
        assert source is None

    def test_duplicate_url_does_not_crash(self):
        _create_registry("duphash")

        persist_source_url("duphash", "https://example.com/dup.png")
        persist_source_url("duphash", "https://example.com/dup.png")

        registry = ImageRegistry.query.filter_by(phash="duphash").first()
        sources = ImageSource.query.filter_by(image_id=registry.id).all()
        assert len(sources) >= 1


class TestBuildVoterId:
    def test_produces_consistent_hash(self):
        id1 = build_voter_id("192.168.1.1")
        id2 = build_voter_id("192.168.1.1")
        assert id1 == id2

    def test_different_ips_produce_different_ids(self):
        id1 = build_voter_id("192.168.1.1")
        id2 = build_voter_id("10.0.0.1")
        assert id1 != id2

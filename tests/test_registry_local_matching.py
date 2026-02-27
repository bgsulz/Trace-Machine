from sqlalchemy.exc import IntegrityError

from conftest import _make_test_image_bytes
from veracity import db
from veracity.models import ImageRegistry
from veracity.registry import (
    LocalMatchSnapshot,
    _get_or_create_registry_entry,
    prepare_analysis_context,
)


def test_prepare_analysis_context_includes_local_only_neighbor(app, monkeypatch):
    with app.app_context():
        candidate = ImageRegistry(
            phash="0123456789abcdef",
            whash="fedcba9876543210",
        )
        db.session.add(candidate)
        db.session.commit()
        candidate_id = candidate.id

        monkeypatch.setattr("veracity.registry._local_matching_enabled", lambda: True)
        monkeypatch.setattr(
            "veracity.registry._load_or_create_registry_features",
            lambda *_args, **_kwargs: None,
        )
        monkeypatch.setattr("veracity.registry._is_hash_match", lambda *_args, **_kwargs: False)

        def _fake_local_eval(*, candidate, **_kwargs):
            if candidate.id == candidate_id:
                return (
                    LocalMatchSnapshot(
                        extractor="orb",
                        good_matches=40,
                        inliers=26,
                        inlier_ratio=0.65,
                        homography_found=True,
                        crop_box=(0.1, 0.1, 0.6, 0.6),
                    ),
                    True,
                )
            return None, False

        monkeypatch.setattr(
            "veracity.registry._evaluate_local_candidate_match",
            _fake_local_eval,
        )

        context = prepare_analysis_context(_make_test_image_bytes())

        neighbors = {neighbor.id: neighbor for neighbor in context.neighbors}
        assert candidate_id in neighbors
        assert neighbors[candidate_id].match_method == "local"
        assert neighbors[candidate_id].local_match is not None


def test_get_or_create_registry_entry_recovers_from_integrity_error(app, monkeypatch):
    with app.app_context():
        phash = "abc123abc123abcd"
        whash = "def456def456def4"
        existing = ImageRegistry(phash=phash, whash=whash)

        class _QueryStub:
            def __init__(self):
                self.first_calls = 0

            def filter_by(self, **_kwargs):
                return self

            def first(self):
                self.first_calls += 1
                if self.first_calls == 1:
                    return None
                return existing

        query_stub = _QueryStub()
        add_calls = {"count": 0}
        rollback_calls = {"count": 0}

        def _fake_add(_row):
            add_calls["count"] += 1

        def _fake_commit():
            raise IntegrityError("insert", {}, Exception("duplicate key"))

        def _fake_rollback():
            rollback_calls["count"] += 1

        monkeypatch.setattr("veracity.registry.ImageRegistry.query", query_stub, raising=False)
        monkeypatch.setattr("veracity.registry.db.session.add", _fake_add)
        monkeypatch.setattr("veracity.registry.db.session.commit", _fake_commit)
        monkeypatch.setattr("veracity.registry.db.session.rollback", _fake_rollback)

        result = _get_or_create_registry_entry(phash, whash)

        assert result is existing
        assert add_calls["count"] == 1
        assert rollback_calls["count"] == 1
        assert query_stub.first_calls == 2

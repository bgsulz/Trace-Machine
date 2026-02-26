from conftest import _make_test_image_bytes
from veracity import db
from veracity.models import ImageRegistry
from veracity.registry import LocalMatchSnapshot, prepare_analysis_context


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

import importlib

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.exc import IntegrityError

from migrations.versions import d7f4c8e9a2b1_generalize_synthid_reports as migration

collapse_migration = importlib.import_module(
    "migrations.versions.9b1c2d3e4f50_collapse_verification_portal_reports"
)


def test_synthid_migration_backfills_and_allows_provider_specific_reports(monkeypatch):
    engine = sa.create_engine("sqlite:///:memory:")
    metadata = sa.MetaData()
    sa.Table(
        "image_registry",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
    )
    synth_id_report = sa.Table(
        "synth_id_report",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("image_id", sa.Integer, nullable=False),
        sa.Column("voter_id", sa.String(64), nullable=False),
        sa.Column("result", sa.String(16), nullable=False),
        sa.UniqueConstraint("image_id", "voter_id", name="uq_synthid_report"),
    )
    metadata.create_all(engine)

    with engine.begin() as conn:
        conn.execute(sa.insert(synth_id_report), {
            "id": 1,
            "image_id": 1,
            "voter_id": "voter-1",
            "result": "detected",
        })

    with engine.begin() as conn:
        context = MigrationContext.configure(conn)
        monkeypatch.setattr(migration, "op", Operations(context))
        migration.upgrade()

        rows = conn.execute(sa.text("SELECT * FROM synth_id_report")).mappings().all()
        assert rows[0]["provider"] == "google"
        assert rows[0]["detector"] == "google_about_this_image"
        assert rows[0]["source_kind"] == "manual_user_report"

        conn.execute(sa.text("""
            INSERT INTO synth_id_report
                (image_id, voter_id, result, provider, detector, source_kind)
            VALUES
                (1, 'voter-1', 'not_detected', 'openai', 'openai_verify', 'manual_user_report')
        """))

        try:
            conn.execute(sa.text("""
                INSERT INTO synth_id_report
                    (image_id, voter_id, result, provider, detector, source_kind)
                VALUES
                    (1, 'voter-1', 'not_detected', 'google', 'google_about_this_image', 'manual_user_report')
            """))
        except IntegrityError:
            pass
        else:  # pragma: no cover - sanity guard
            raise AssertionError("expected duplicate provider/detector report to fail")


def test_portal_collapse_migration_normalizes_and_deduplicates(monkeypatch):
    engine = sa.create_engine("sqlite:///:memory:")
    metadata = sa.MetaData()
    sa.Table(
        "image_registry",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
    )
    synth_id_report = sa.Table(
        "synth_id_report",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("image_id", sa.Integer, nullable=False),
        sa.Column("voter_id", sa.String(64), nullable=False),
        sa.Column("result", sa.String(16), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("detector", sa.String(64), nullable=False),
        sa.Column("source_kind", sa.String(32), nullable=False),
        sa.UniqueConstraint(
            "image_id",
            "voter_id",
            "provider",
            "detector",
            name="uq_synthid_report_detector",
        ),
    )
    metadata.create_all(engine)

    rows = [
        {
            "id": 1,
            "image_id": 1,
            "voter_id": "conflict",
            "result": "not_detected",
            "provider": "google",
            "detector": "google_about_this_image",
            "source_kind": "manual_user_report",
        },
        {
            "id": 2,
            "image_id": 1,
            "voter_id": "conflict",
            "result": "detected",
            "provider": "openai",
            "detector": "openai_verify",
            "source_kind": "manual_user_report",
        },
        {
            "id": 3,
            "image_id": 2,
            "voter_id": "both-positive",
            "result": "detected",
            "provider": "google",
            "detector": "google_about_this_image",
            "source_kind": "manual_user_report",
        },
        {
            "id": 4,
            "image_id": 2,
            "voter_id": "both-positive",
            "result": "detected",
            "provider": "openai",
            "detector": "openai_verify",
            "source_kind": "manual_user_report",
        },
        {
            "id": 5,
            "image_id": 3,
            "voter_id": "negative",
            "result": "not_detected",
            "provider": "google",
            "detector": "google_about_this_image",
            "source_kind": "manual_user_report",
        },
    ]
    with engine.begin() as conn:
        conn.execute(sa.insert(synth_id_report), rows)

    with engine.begin() as conn:
        context = MigrationContext.configure(conn)
        monkeypatch.setattr(collapse_migration, "op", Operations(context))
        collapse_migration.upgrade()

        kept = conn.execute(
            sa.text("SELECT id, image_id, voter_id, result FROM synth_id_report ORDER BY id")
        ).mappings().all()
        assert kept == [
            {"id": 2, "image_id": 1, "voter_id": "conflict", "result": "openai_positive"},
            {"id": 3, "image_id": 2, "voter_id": "both-positive", "result": "google_positive"},
            {"id": 5, "image_id": 3, "voter_id": "negative", "result": "negative"},
        ]

        columns = {
            row["name"]
            for row in conn.execute(sa.text("PRAGMA table_info(synth_id_report)")).mappings()
        }
        assert {"provider", "detector", "source_kind"}.isdisjoint(columns)

        try:
            conn.execute(
                sa.text(
                    """
                    INSERT INTO synth_id_report (image_id, voter_id, result)
                    VALUES (1, 'conflict', 'negative')
                    """
                )
            )
        except IntegrityError:
            pass
        else:  # pragma: no cover - sanity guard
            raise AssertionError("expected duplicate image/voter portal report to fail")

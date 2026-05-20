import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.exc import IntegrityError

from migrations.versions import d7f4c8e9a2b1_generalize_synthid_reports as migration


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

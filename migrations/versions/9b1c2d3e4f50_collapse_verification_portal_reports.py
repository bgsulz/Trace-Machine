"""Collapse verification portal reports

Revision ID: 9b1c2d3e4f50
Revises: d7f4c8e9a2b1
Create Date: 2026-07-08 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "9b1c2d3e4f50"
down_revision = "d7f4c8e9a2b1"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        UPDATE synth_id_report
        SET result = CASE
            WHEN result = 'not_detected' THEN 'negative'
            WHEN result = 'detected'
                 AND (provider = 'openai' OR detector = 'openai_verify')
                THEN 'openai_positive'
            WHEN result = 'detected' THEN 'google_positive'
            ELSE result
        END
        """
    )

    op.execute(
        """
        DELETE FROM synth_id_report
        WHERE id NOT IN (
            SELECT id
            FROM (
                SELECT
                    id,
                    ROW_NUMBER() OVER (
                        PARTITION BY image_id, voter_id
                        ORDER BY
                            CASE
                                WHEN result IN ('google_positive', 'openai_positive') THEN 0
                                ELSE 1
                            END,
                            id
                    ) AS row_number
                FROM synth_id_report
            ) ranked
            WHERE row_number = 1
        )
        """
    )

    with op.batch_alter_table("synth_id_report") as batch_op:
        batch_op.drop_constraint("uq_synthid_report_detector", type_="unique")
        batch_op.alter_column(
            "result",
            existing_type=sa.String(length=16),
            type_=sa.String(length=32),
            existing_nullable=False,
        )
        batch_op.drop_column("source_kind")
        batch_op.drop_column("detector")
        batch_op.drop_column("provider")
        batch_op.create_unique_constraint(
            "uq_synthid_report",
            ["image_id", "voter_id"],
        )


def downgrade():
    with op.batch_alter_table("synth_id_report") as batch_op:
        batch_op.drop_constraint("uq_synthid_report", type_="unique")
        batch_op.add_column(sa.Column("provider", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("detector", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("source_kind", sa.String(length=32), nullable=True))

    op.execute(
        """
        UPDATE synth_id_report
        SET provider = CASE
                WHEN result = 'openai_positive' THEN 'openai'
                ELSE 'google'
            END,
            detector = CASE
                WHEN result = 'openai_positive' THEN 'openai_verify'
                ELSE 'google_about_this_image'
            END,
            source_kind = 'manual_user_report',
            result = CASE
                WHEN result IN ('google_positive', 'openai_positive') THEN 'detected'
                ELSE 'not_detected'
            END
        """
    )

    with op.batch_alter_table("synth_id_report") as batch_op:
        batch_op.alter_column(
            "provider",
            existing_type=sa.String(length=32),
            nullable=False,
        )
        batch_op.alter_column(
            "detector",
            existing_type=sa.String(length=64),
            nullable=False,
        )
        batch_op.alter_column(
            "source_kind",
            existing_type=sa.String(length=32),
            nullable=False,
        )
        batch_op.alter_column(
            "result",
            existing_type=sa.String(length=32),
            type_=sa.String(length=16),
            existing_nullable=False,
        )
        batch_op.create_unique_constraint(
            "uq_synthid_report_detector",
            ["image_id", "voter_id", "provider", "detector"],
        )

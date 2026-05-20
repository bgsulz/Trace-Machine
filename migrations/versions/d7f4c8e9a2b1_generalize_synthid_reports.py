"""Generalize SynthID reports by provider and detector

Revision ID: d7f4c8e9a2b1
Revises: c4b85f2a1d10
Create Date: 2026-05-20 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "d7f4c8e9a2b1"
down_revision = "c4b85f2a1d10"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("synth_id_report") as batch_op:
        batch_op.add_column(sa.Column("provider", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("detector", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("source_kind", sa.String(length=32), nullable=True))

    op.execute(
        """
        UPDATE synth_id_report
        SET provider = 'google',
            detector = 'google_about_this_image',
            source_kind = 'manual_user_report'
        WHERE provider IS NULL
           OR detector IS NULL
           OR source_kind IS NULL
        """
    )

    with op.batch_alter_table("synth_id_report") as batch_op:
        batch_op.drop_constraint("uq_synthid_report", type_="unique")
        batch_op.alter_column("provider", existing_type=sa.String(length=32), nullable=False)
        batch_op.alter_column("detector", existing_type=sa.String(length=64), nullable=False)
        batch_op.alter_column("source_kind", existing_type=sa.String(length=32), nullable=False)
        batch_op.create_unique_constraint(
            "uq_synthid_report_detector",
            ["image_id", "voter_id", "provider", "detector"],
        )


def downgrade():
    op.execute(
        """
        DELETE FROM synth_id_report
        WHERE id NOT IN (
            SELECT MIN(id)
            FROM synth_id_report
            GROUP BY image_id, voter_id
        )
        """
    )

    with op.batch_alter_table("synth_id_report") as batch_op:
        batch_op.drop_constraint("uq_synthid_report_detector", type_="unique")
        batch_op.create_unique_constraint(
            "uq_synthid_report",
            ["image_id", "voter_id"],
        )
        batch_op.drop_column("source_kind")
        batch_op.drop_column("detector")
        batch_op.drop_column("provider")

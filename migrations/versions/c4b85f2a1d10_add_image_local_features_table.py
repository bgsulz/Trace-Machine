"""Add image_local_features table for keypoint descriptors

Revision ID: c4b85f2a1d10
Revises: f0d327233827
Create Date: 2026-02-26 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "c4b85f2a1d10"
down_revision = "f0d327233827"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "image_local_features",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("image_id", sa.Integer(), nullable=False),
        sa.Column("extractor", sa.String(length=16), nullable=False),
        sa.Column("feature_version", sa.Integer(), nullable=False),
        sa.Column("keypoint_count", sa.Integer(), nullable=False),
        sa.Column("payload", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["image_id"], ["image_registry.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("image_id"),
    )


def downgrade():
    op.drop_table("image_local_features")

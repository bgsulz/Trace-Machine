"""Add filtered count to TinEye

Revision ID: 081ea53e0d97
Revises: 70fba85f0669
Create Date: 2026-01-13 23:40:30.872291

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "081ea53e0d97"
down_revision = "70fba85f0669"
branch_labels = None
depends_on = None


def _column_exists(connection, table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(connection)
    columns = {col["name"] for col in inspector.get_columns(table_name)}
    return column_name in columns


def upgrade():
    conn = op.get_bind()
    if _column_exists(conn, "tineye_result", "filtered_match_count"):
        return

    with op.batch_alter_table("tineye_result") as batch_op:
        batch_op.add_column(
            sa.Column(
                "filtered_match_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("-1"),
            )
        )
        batch_op.alter_column("filtered_match_count", server_default=None)


def downgrade():
    conn = op.get_bind()
    if not _column_exists(conn, "tineye_result", "filtered_match_count"):
        return

    with op.batch_alter_table("tineye_result") as batch_op:
        batch_op.drop_column("filtered_match_count")

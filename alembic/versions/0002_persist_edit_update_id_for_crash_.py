"""Persist edit update id for crash recovery

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "pending_transactions", sa.Column("last_edit_update_id", sa.BigInteger(), nullable=True)
    )
    op.create_unique_constraint(
        "uq_pending_last_edit_update_id", "pending_transactions", ["last_edit_update_id"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_pending_last_edit_update_id", "pending_transactions", type_="unique")
    op.drop_column("pending_transactions", "last_edit_update_id")

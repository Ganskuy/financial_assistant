"""Confirmed opening money, separate from income and expenses."""

import sqlalchemy as sa

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("pending_transactions_kind_check", "pending_transactions", type_="check")
    op.create_check_constraint(
        "pending_transactions_kind_check",
        "pending_transactions",
        "kind IN ('transaction','budget','goal','saving','opening')",
    )
    op.create_table(
        "opening_balances",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("pending_id", sa.UUID(), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["pending_id"], ["pending_transactions.id"]),
        sa.UniqueConstraint("user_id"),
        sa.UniqueConstraint("pending_id"),
        sa.CheckConstraint("amount >= 0 AND amount <= 1000000000000"),
    )
    op.execute(
        "CREATE TRIGGER immutable_ledger BEFORE UPDATE OR DELETE ON opening_balances FOR EACH ROW EXECUTE FUNCTION reject_ledger_mutation()"
    )


def downgrade() -> None:
    # Never silently discard an opening amount or an audit record during rollback.
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM opening_balances) OR
           EXISTS (SELECT 1 FROM pending_transactions WHERE kind = 'opening') THEN
            RAISE EXCEPTION 'Opening balance data exists; refusing destructive downgrade';
        END IF;
        END $$""")
    op.drop_table("opening_balances")
    op.drop_constraint("pending_transactions_kind_check", "pending_transactions", type_="check")
    op.create_check_constraint(
        "pending_transactions_kind_check",
        "pending_transactions",
        "kind IN ('transaction','budget','goal','saving')",
    )

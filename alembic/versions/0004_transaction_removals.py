"""Confirmation-gated, append-only transaction removals."""

import sqlalchemy as sa

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("pending_transactions_kind_check", "pending_transactions", type_="check")
    op.create_check_constraint(
        "pending_transactions_kind_check",
        "pending_transactions",
        "kind IN ('transaction','budget','goal','saving','opening','removal')",
    )
    op.drop_constraint("callback_actions_action_check", "callback_actions", type_="check")
    op.create_check_constraint(
        "callback_actions_action_check",
        "callback_actions",
        "action IN ('confirm','edit','cancel','select')",
    )
    op.add_column("callback_actions", sa.Column("target_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "callback_actions_target_id_fkey", "callback_actions", "transactions", ["target_id"], ["id"]
    )
    op.create_table(
        "transaction_removals",
        sa.Column("transaction_id", sa.UUID(), sa.ForeignKey("transactions.id"), primary_key=True),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "pending_id",
            sa.UUID(),
            sa.ForeignKey("pending_transactions.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.execute(
        "CREATE TRIGGER immutable_ledger BEFORE UPDATE OR DELETE ON transaction_removals "
        "FOR EACH ROW EXECUTE FUNCTION reject_ledger_mutation()"
    )


def downgrade() -> None:
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM pending_transactions WHERE kind = 'removal') OR
           EXISTS (SELECT 1 FROM transaction_removals) THEN
            RAISE EXCEPTION 'Removal data exists; refusing destructive downgrade';
        END IF;
        END $$""")
    op.drop_table("transaction_removals")
    op.drop_column("callback_actions", "target_id")
    op.drop_constraint("callback_actions_action_check", "callback_actions", type_="check")
    op.create_check_constraint(
        "callback_actions_action_check", "callback_actions", "action IN ('confirm','edit','cancel')"
    )
    op.drop_constraint("pending_transactions_kind_check", "pending_transactions", type_="check")
    op.create_check_constraint(
        "pending_transactions_kind_check",
        "pending_transactions",
        "kind IN ('transaction','budget','goal','saving','opening')",
    )

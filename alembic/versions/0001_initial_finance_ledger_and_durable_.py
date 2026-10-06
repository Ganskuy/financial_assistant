"""Initial finance ledger and durable workflow schema

Revision ID: 0001
Revises:
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "daily_llm_usage",
        sa.Column("usage_date", sa.Date(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("reserved_tokens", sa.Integer(), nullable=False),
        sa.Column("total_tokens", sa.Integer(), nullable=False),
        sa.Column("blocked", sa.Boolean(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0 AND reserved_tokens >= 0 AND total_tokens >= 0"
        ),
        sa.PrimaryKeyConstraint("usage_date"),
    )
    op.create_table(
        "processed_telegram_updates",
        sa.Column("update_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("event_key", sa.String(length=280), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("response", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("update_id"),
        sa.UniqueConstraint("telegram_user_id", "event_key"),
    )
    op.create_index(
        op.f("ix_processed_telegram_updates_next_attempt_at"),
        "processed_telegram_updates",
        ["next_attempt_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_processed_telegram_updates_received_at"),
        "processed_telegram_updates",
        ["received_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_processed_telegram_updates_status"),
        "processed_telegram_updates",
        ["status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_processed_telegram_updates_telegram_user_id"),
        "processed_telegram_updates",
        ["telegram_user_id"],
        unique=False,
    )
    op.create_table(
        "rate_buckets",
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("minute", sa.BigInteger(), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("telegram_user_id"),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("telegram_user_id"),
    )
    op.create_table(
        "budgets",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("month", sa.Date(), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("amount > 0"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "month", "category"),
    )
    op.create_table(
        "llm_reservations",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("usage_date", sa.Date(), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("status IN ('reserved','settled','unknown')"),
        sa.CheckConstraint("amount > 0"),
        sa.ForeignKeyConstraint(
            ["usage_date"],
            ["daily_llm_usage.usage_date"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_llm_reservations_status"), "llm_reservations", ["status"], unique=False
    )
    op.create_table(
        "pending_transactions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("source_update_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("warnings", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("input_source", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("kind IN ('transaction','budget','goal','saving')"),
        sa.CheckConstraint("status IN ('pending','editing','confirmed','cancelled')"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_update_id"),
    )
    op.create_index(
        op.f("ix_pending_transactions_expires_at"),
        "pending_transactions",
        ["expires_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_pending_transactions_user_id"), "pending_transactions", ["user_id"], unique=False
    )
    op.create_table(
        "savings_goals",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("target", sa.BigInteger(), nullable=False),
        sa.Column("deadline", sa.Date(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("target > 0"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "name"),
    )
    op.create_index(op.f("ix_savings_goals_user_id"), "savings_goals", ["user_id"], unique=False)
    op.create_table(
        "callback_actions",
        sa.Column("token", sa.String(length=32), nullable=False),
        sa.Column("pending_id", sa.UUID(), nullable=False),
        sa.Column("action", sa.String(length=8), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint("action IN ('confirm','edit','cancel')"),
        sa.ForeignKeyConstraint(
            ["pending_id"],
            ["pending_transactions.id"],
        ),
        sa.PrimaryKeyConstraint("token"),
    )
    op.create_index(
        op.f("ix_callback_actions_pending_id"), "callback_actions", ["pending_id"], unique=False
    )
    op.create_table(
        "savings_contributions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("goal_id", sa.UUID(), nullable=False),
        sa.Column("pending_id", sa.UUID(), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("amount > 0"),
        sa.ForeignKeyConstraint(
            ["goal_id"],
            ["savings_goals.id"],
        ),
        sa.ForeignKeyConstraint(
            ["pending_id"],
            ["pending_transactions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("pending_id"),
    )
    op.create_index(
        op.f("ix_savings_contributions_goal_id"), "savings_contributions", ["goal_id"], unique=False
    )
    op.create_index(
        op.f("ix_savings_contributions_user_id"), "savings_contributions", ["user_id"], unique=False
    )
    op.create_table(
        "transactions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("pending_id", sa.UUID(), nullable=False),
        sa.Column("type", sa.String(length=8), nullable=False),
        sa.Column("transaction_date", sa.Date(), nullable=False),
        sa.Column("merchant_or_source", sa.String(length=120), nullable=True),
        sa.Column("description", sa.String(length=240), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("input_source", sa.String(length=16), nullable=False),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("currency = 'IDR'"),
        sa.CheckConstraint("type IN ('income','expense')"),
        sa.CheckConstraint("amount > 0 AND amount <= 1000000000000"),
        sa.ForeignKeyConstraint(
            ["pending_id"],
            ["pending_transactions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("pending_id"),
        sa.UniqueConstraint("user_id", "telegram_message_id"),
    )
    op.create_index(
        "ix_transactions_user_category_date",
        "transactions",
        ["user_id", "category", "transaction_date"],
        unique=False,
    )
    op.create_index(
        "ix_transactions_user_date", "transactions", ["user_id", "transaction_date"], unique=False
    )
    op.create_index(
        "ix_transactions_user_type_date",
        "transactions",
        ["user_id", "type", "transaction_date"],
        unique=False,
    )
    op.create_table(
        "transaction_items",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("transaction_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=12, scale=3), nullable=False),
        sa.Column("unit_price", sa.BigInteger(), nullable=False),
        sa.Column("subtotal", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("quantity > 0 AND unit_price >= 0 AND subtotal >= 0"),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["transactions.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_transaction_items_transaction_id"),
        "transaction_items",
        ["transaction_id"],
        unique=False,
    )

    # Static DDL only: financial ledger rows cannot be changed after insertion.
    op.execute("""CREATE FUNCTION reject_ledger_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
        RAISE EXCEPTION 'Ledger records are immutable'; END $$""")
    for table in ("transactions", "transaction_items", "savings_contributions"):
        op.execute(
            sa.text(
                'CREATE TRIGGER immutable_ledger BEFORE UPDATE OR DELETE ON "'
                + table
                + '" FOR EACH ROW EXECUTE FUNCTION reject_ledger_mutation()'
            )
        )


def downgrade() -> None:
    op.drop_index(op.f("ix_transaction_items_transaction_id"), table_name="transaction_items")
    op.drop_table("transaction_items")
    op.drop_index("ix_transactions_user_type_date", table_name="transactions")
    op.drop_index("ix_transactions_user_date", table_name="transactions")
    op.drop_index("ix_transactions_user_category_date", table_name="transactions")
    op.drop_table("transactions")
    op.drop_index(op.f("ix_savings_contributions_user_id"), table_name="savings_contributions")
    op.drop_index(op.f("ix_savings_contributions_goal_id"), table_name="savings_contributions")
    op.drop_table("savings_contributions")
    op.drop_index(op.f("ix_callback_actions_pending_id"), table_name="callback_actions")
    op.drop_table("callback_actions")
    op.drop_index(op.f("ix_savings_goals_user_id"), table_name="savings_goals")
    op.drop_table("savings_goals")
    op.drop_index(op.f("ix_pending_transactions_user_id"), table_name="pending_transactions")
    op.drop_index(op.f("ix_pending_transactions_expires_at"), table_name="pending_transactions")
    op.drop_table("pending_transactions")
    op.drop_index(op.f("ix_llm_reservations_status"), table_name="llm_reservations")
    op.drop_table("llm_reservations")
    op.drop_table("budgets")
    op.drop_table("users")
    op.drop_table("rate_buckets")
    op.drop_index(
        op.f("ix_processed_telegram_updates_telegram_user_id"),
        table_name="processed_telegram_updates",
    )
    op.drop_index(
        op.f("ix_processed_telegram_updates_status"), table_name="processed_telegram_updates"
    )
    op.drop_index(
        op.f("ix_processed_telegram_updates_received_at"), table_name="processed_telegram_updates"
    )
    op.drop_index(
        op.f("ix_processed_telegram_updates_next_attempt_at"),
        table_name="processed_telegram_updates",
    )
    op.drop_table("processed_telegram_updates")
    op.drop_table("daily_llm_usage")
    op.execute("DROP FUNCTION reject_ledger_mutation()")

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PendingTransaction(Base):
    __tablename__ = "pending_transactions"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    source_update_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    last_edit_update_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    telegram_message_id: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(16))
    payload: Mapped[dict] = mapped_column(JSONB)
    warnings: Mapped[list] = mapped_column(JSONB, default=list)
    input_source: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    version: Mapped[int] = mapped_column(Integer, default=1)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        CheckConstraint("status IN ('pending','editing','confirmed','cancelled')"),
        CheckConstraint(
            "kind IN ('transaction','budget','goal','saving','opening')",
            name="pending_transactions_kind_check",
        ),
    )


class CallbackAction(Base):
    __tablename__ = "callback_actions"
    token: Mapped[str] = mapped_column(String(32), primary_key=True)
    pending_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("pending_transactions.id"), index=True)
    action: Mapped[str] = mapped_column(String(8))
    version: Mapped[int] = mapped_column(Integer)
    __table_args__ = (CheckConstraint("action IN ('confirm','edit','cancel')"),)


class Transaction(Base):
    __tablename__ = "transactions"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    pending_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pending_transactions.id"), unique=True
    )
    type: Mapped[str] = mapped_column(String(8))
    transaction_date: Mapped[date] = mapped_column(Date)
    merchant_or_source: Mapped[str | None] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(String(240))
    category: Mapped[str] = mapped_column(String(32))
    amount: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3), default="IDR")
    input_source: Mapped[str] = mapped_column(String(16))
    telegram_message_id: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        CheckConstraint("amount > 0 AND amount <= 1000000000000"),
        CheckConstraint("currency = 'IDR'"),
        CheckConstraint("type IN ('income','expense')"),
        UniqueConstraint("user_id", "telegram_message_id"),
        Index("ix_transactions_user_date", "user_id", "transaction_date"),
        Index("ix_transactions_user_type_date", "user_id", "type", "transaction_date"),
        Index("ix_transactions_user_category_date", "user_id", "category", "transaction_date"),
    )


class TransactionItem(Base):
    __tablename__ = "transaction_items"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    transaction_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("transactions.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3))
    unit_price: Mapped[int] = mapped_column(BigInteger)
    subtotal: Mapped[int] = mapped_column(BigInteger)
    __table_args__ = (CheckConstraint("quantity > 0 AND unit_price >= 0 AND subtotal >= 0"),)


class Budget(Base):
    __tablename__ = "budgets"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    month: Mapped[date] = mapped_column(Date)
    category: Mapped[str] = mapped_column(String(32))
    amount: Mapped[int] = mapped_column(BigInteger)
    __table_args__ = (
        UniqueConstraint("user_id", "month", "category"),
        CheckConstraint("amount > 0"),
    )


class SavingsGoal(Base):
    __tablename__ = "savings_goals"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    target: Mapped[int] = mapped_column(BigInteger)
    deadline: Mapped[date] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (UniqueConstraint("user_id", "name"), CheckConstraint("target > 0"))


class SavingsContribution(Base):
    __tablename__ = "savings_contributions"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    goal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("savings_goals.id"), index=True)
    pending_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pending_transactions.id"), unique=True
    )
    amount: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (CheckConstraint("amount > 0"),)


class DailyLLMUsage(Base):
    __tablename__ = "daily_llm_usage"
    usage_date: Mapped[date] = mapped_column(Date, primary_key=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    reserved_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    blocked: Mapped[bool] = mapped_column(default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    __table_args__ = (
        CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0 AND reserved_tokens >= 0 AND total_tokens >= 0"
        ),
    )


class LLMReservation(Base):
    __tablename__ = "llm_reservations"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    usage_date: Mapped[date] = mapped_column(ForeignKey("daily_llm_usage.usage_date"))
    amount: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(12), default="reserved", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        CheckConstraint("amount > 0"),
        CheckConstraint("status IN ('reserved','settled','unknown')"),
    )


class ProcessedUpdate(Base):
    __tablename__ = "processed_telegram_updates"
    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int] = mapped_column(BigInteger)
    event_key: Mapped[str] = mapped_column(String(280))
    payload: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(12), default="queued", index=True)
    response: Mapped[dict | None] = mapped_column(JSONB)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("telegram_user_id", "event_key"),)


class RateBucket(Base):
    __tablename__ = "rate_buckets"
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    minute: Mapped[int] = mapped_column(BigInteger)
    count: Mapped[int] = mapped_column(Integer)


class OpeningBalance(Base):
    __tablename__ = "opening_balances"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), unique=True)
    pending_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pending_transactions.id"), unique=True
    )
    amount: Mapped[int] = mapped_column(BigInteger)
    as_of: Mapped[date] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (CheckConstraint("amount >= 0 AND amount <= 1000000000000"),)

import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from app.core.db import Database
from app.core.errors import BudgetExceeded
from app.models.tables import DailyLLMUsage, LLMReservation

# All replicas use the same PostgreSQL transaction-level mutex. No network calls under it.
BUDGET_LOCK = 723401981


class TokenBudget:
    def __init__(
        self, db: Database, limit: int = 20000, clock: Callable[[], datetime] | None = None
    ):
        if not 1 <= limit <= 20000:
            raise ValueError("Hard cap cannot exceed 20000")
        self.db, self.limit = db, limit
        self.clock = clock or (lambda: datetime.now(UTC))

    def day(self) -> date:
        return self.clock().astimezone(ZoneInfo("Asia/Jakarta")).date()

    async def _day_row(self, session, day: date) -> DailyLLMUsage:
        await session.execute(
            insert(DailyLLMUsage)
            .values(
                usage_date=day,
                input_tokens=0,
                output_tokens=0,
                reserved_tokens=0,
                total_tokens=0,
                blocked=False,
            )
            .on_conflict_do_nothing()
        )
        return await session.scalar(
            select(DailyLLMUsage).where(DailyLLMUsage.usage_date == day).with_for_update()
        )

    async def _lock(self, session) -> None:
        await session.execute(select(func.pg_advisory_xact_lock(BUDGET_LOCK)))

    async def _carried(self, session, day: date) -> int:
        return int(
            await session.scalar(
                select(func.coalesce(func.sum(LLMReservation.amount), 0)).where(
                    LLMReservation.usage_date != day,
                    LLMReservation.status.in_(["reserved", "unknown"]),
                )
            )
        )

    async def reserve(self, amount: int) -> uuid.UUID:
        if type(amount) is not int or not 1 <= amount <= self.limit:
            raise BudgetExceeded()
        async with self.db.transaction() as session:
            await self._lock(session)
            day = self.day()
            usage = await self._day_row(session, day)
            carried = await self._carried(session, day)
            breached = await session.scalar(
                select(DailyLLMUsage.usage_date).where(DailyLLMUsage.blocked.is_(True)).limit(1)
            )
            if (
                breached
                or usage.total_tokens + usage.reserved_tokens + carried + amount > self.limit
            ):
                raise BudgetExceeded()
            usage.reserved_tokens += amount
            reservation = LLMReservation(usage_date=day, amount=amount)
            session.add(reservation)
            await session.flush()
            return reservation.id

    async def unknown(self, reservation_id: uuid.UUID) -> None:
        # May already have reached the provider; never refund on HTTP failure, timeout or missing usage.
        async with self.db.transaction() as session:
            await self._lock(session)
            reservation = await session.get(LLMReservation, reservation_id)
            if reservation and reservation.status == "reserved":
                reservation.status = "unknown"

    async def reconcile(
        self, reservation_id: uuid.UUID, input_tokens: int, output_tokens: int
    ) -> None:
        if any(type(v) is not int or v < 0 for v in (input_tokens, output_tokens)):
            raise ValueError("Invalid provider usage")
        breach = False
        async with self.db.transaction() as session:
            await self._lock(session)
            reservation = await session.get(LLMReservation, reservation_id)
            if reservation is None:
                raise ValueError("Unknown reservation")
            if reservation.status == "settled":
                return
            original = await self._day_row(session, reservation.usage_date)
            original.reserved_tokens -= reservation.amount
            actual = input_tokens + output_tokens
            original.input_tokens += input_tokens
            original.output_tokens += output_tokens
            original.total_tokens += actual
            reservation.status = "settled"
            # Cross-midnight requests charge both dates conservatively. No midnight loophole.
            current_day = self.day()
            if current_day != reservation.usage_date:
                current = await self._day_row(session, current_day)
                current.input_tokens += input_tokens
                current.output_tokens += output_tokens
                current.total_tokens += actual
            if actual > reservation.amount:
                original.blocked = True
                breach = True
        if breach:
            raise BudgetExceeded()

    async def usage(self) -> dict:
        async with self.db.transaction() as session:
            await self._lock(session)
            day = self.day()
            usage = await self._day_row(session, day)
            reserved = usage.reserved_tokens + await self._carried(session, day)
            return {
                "day": str(day),
                "used": usage.total_tokens,
                "reserved": reserved,
                "limit": self.limit,
                "remaining": max(0, self.limit - usage.total_tokens - reserved),
            }

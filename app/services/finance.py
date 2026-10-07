from datetime import timedelta
from uuid import UUID

from app.core.db import Database
from app.repositories.finance import FinanceRepository
from app.services.validation import idr, month_bounds, today


class FinanceService:
    def __init__(self, db: Database, user_id: UUID):
        self.db, self.user_id = db, user_id

    async def summary(self, period: str | None = None) -> dict:
        start, end = month_bounds(period)
        async with self.db.transaction() as session:
            return await FinanceRepository(session, self.user_id).summary(start, end)

    async def current_balance(self) -> dict:
        async with self.db.transaction() as session:
            return await FinanceRepository(session, self.user_id).current_balance(today())

    async def recent(self, *, limit: int = 10, offset: int = 0) -> list[dict]:
        async with self.db.transaction() as session:
            rows = await FinanceRepository(session, self.user_id).recent(limit, offset)
            return [
                {
                    "date": r.transaction_date.isoformat(),
                    "type": r.type,
                    "amount": r.amount,
                    "category": r.category,
                    "description": r.description,
                    "merchant": r.merchant_or_source,
                }
                for r in rows
            ]

    async def budgets(self, period: str | None = None) -> list[dict]:
        start, end = month_bounds(period)
        async with self.db.transaction() as session:
            return await FinanceRepository(session, self.user_id).budgets(start, end)

    async def savings(self) -> list[dict]:
        async with self.db.transaction() as session:
            return await FinanceRepository(session, self.user_id).goals()

    async def advisor_context(self, period: str | None = None) -> dict:
        """Fetch only aggregated facts, reusing the monthly spending calculation."""
        start, end = month_bounds(period)
        async with self.db.transaction() as session:
            repository = FinanceRepository(session, self.user_id)
            summary = await repository.summary(start, end)
            if summary["income"] == 0 and summary["expenses"] == 0:
                return {"summary": summary, "budgets": [], "savings": []}
            budgets = await repository.budgets(start, end, spending=summary["by_category"])
            savings = await repository.goals(limit=10)
            return {"summary": summary, "budgets": budgets, "savings": savings}

    async def trend(self, period: str | None = None) -> dict:
        start, _ = month_bounds(period)
        previous = (start - timedelta(days=1)).strftime("%Y-%m")
        return {"current": await self.summary(period), "previous": await self.summary(previous)}


def format_summary(data: dict) -> str:
    lines = [
        f"Financial report {data['period']}",
        f"Income: {idr(data['income'])}",
        f"Expenses: {idr(data['expenses'])}",
        f"Monthly net change: {idr(data['balance'])}",
    ]
    lines.extend(f"{category}: {idr(amount)}" for category, amount in data["by_category"].items())
    return "\n".join(lines)


def format_balance(data: dict) -> str:
    if data["opening"] is None:
        return (
            f"Recorded net change: {idr(data['net_change'])}\n"
            "No opening balance is recorded, so this is not your full cash balance. "
            "Before recording any income/expenses, use /opening AMOUNT to initialize it."
        )
    return (
        f"Current tracked balance: {idr(data['balance'])}\n"
        f"Opening balance ({data['as_of']}): {idr(data['opening'])}\n"
        f"Income minus expenses since opening: {idr(data['net_change'])}\n"
        "Based on confirmed records; this is not a live bank balance."
    )

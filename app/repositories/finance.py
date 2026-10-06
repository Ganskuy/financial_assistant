from datetime import date
from uuid import UUID

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tables import Budget, OpeningBalance, SavingsContribution, SavingsGoal, Transaction


class FinanceRepository:
    """A repository is permanently bound to backend-authenticated identity."""

    def __init__(self, session: AsyncSession, user_id: UUID):
        self.session, self.user_id = session, user_id

    async def summary(self, start: date, end: date) -> dict:
        filters = (
            Transaction.user_id == self.user_id,
            Transaction.transaction_date >= start,
            Transaction.transaction_date < end,
        )
        income, expenses = (
            await self.session.execute(
                select(
                    func.coalesce(
                        func.sum(case((Transaction.type == "income", Transaction.amount), else_=0)),
                        0,
                    ),
                    func.coalesce(
                        func.sum(
                            case((Transaction.type == "expense", Transaction.amount), else_=0)
                        ),
                        0,
                    ),
                ).where(*filters)
            )
        ).one()
        rows = (
            await self.session.execute(
                select(Transaction.category, func.sum(Transaction.amount))
                .where(*filters, Transaction.type == "expense")
                .group_by(Transaction.category)
                .order_by(Transaction.category)
            )
        ).all()
        return {
            "period": start.strftime("%Y-%m"),
            "income": int(income),
            "expenses": int(expenses),
            "balance": int(income - expenses),
            "by_category": {k: int(v) for k, v in rows},
        }

    async def current_balance(self, on_date: date) -> dict:
        opening = await self.session.scalar(
            select(OpeningBalance).where(OpeningBalance.user_id == self.user_id)
        )
        filters = [Transaction.user_id == self.user_id, Transaction.transaction_date <= on_date]
        if opening:
            filters.append(Transaction.transaction_date >= opening.as_of)
        net = int(
            await self.session.scalar(
                select(
                    func.coalesce(
                        func.sum(
                            case(
                                (Transaction.type == "income", Transaction.amount),
                                else_=-Transaction.amount,
                            )
                        ),
                        0,
                    )
                ).where(*filters)
            )
        )
        return {
            "opening": opening.amount if opening else None,
            "as_of": opening.as_of.isoformat() if opening else None,
            "net_change": net,
            "balance": (opening.amount if opening else 0) + net,
        }

    async def recent(self, limit: int = 10) -> list[Transaction]:
        return list(
            (
                await self.session.scalars(
                    select(Transaction)
                    .where(Transaction.user_id == self.user_id)
                    .order_by(
                        Transaction.transaction_date.desc(),
                        Transaction.created_at.desc(),
                        Transaction.id,
                    )
                    .limit(min(max(limit, 1), 20))
                )
            ).all()
        )

    async def budgets(self, start: date, end: date) -> list[dict]:
        rows = (
            await self.session.scalars(
                select(Budget)
                .where(Budget.user_id == self.user_id, Budget.month == start)
                .order_by(Budget.category)
            )
        ).all()
        spending = (await self.summary(start, end))["by_category"]
        return [
            {
                "category": r.category,
                "limit": r.amount,
                "spent": spending.get(r.category, 0),
                "remaining": r.amount - spending.get(r.category, 0),
            }
            for r in rows
        ]

    async def goals(self) -> list[dict]:
        rows = (
            await self.session.execute(
                select(SavingsGoal, func.coalesce(func.sum(SavingsContribution.amount), 0))
                .outerjoin(
                    SavingsContribution,
                    (SavingsContribution.goal_id == SavingsGoal.id)
                    & (SavingsContribution.user_id == self.user_id),
                )
                .where(SavingsGoal.user_id == self.user_id)
                .group_by(SavingsGoal.id)
                .order_by(SavingsGoal.created_at)
            )
        ).all()
        return [
            {
                "name": g.name,
                "target": g.target,
                "saved": int(saved),
                "remaining": max(0, g.target - int(saved)),
                "deadline": g.deadline.isoformat(),
            }
            for g, saved in rows
        ]

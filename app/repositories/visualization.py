from datetime import date
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tables import Transaction, active_transaction


class VisualizationRepository:
    """One snapshot/query, bounded daily/category groups, no descriptions or merchants."""

    def __init__(self, session: AsyncSession, user_id: UUID):
        self.session, self.user_id = session, user_id

    async def grouped(self, start: date, end: date) -> list[tuple]:
        statement = (
            select(
                Transaction.transaction_date,
                Transaction.type,
                Transaction.category,
                func.sum(Transaction.amount),
                func.count(Transaction.id),
            )
            .where(
                Transaction.user_id == self.user_id,
                active_transaction(),
                Transaction.transaction_date >= start,
                Transaction.transaction_date < end,
            )
            .group_by(Transaction.transaction_date, Transaction.type, Transaction.category)
            .order_by(Transaction.transaction_date, Transaction.type, Transaction.category)
        )
        return [tuple(row) for row in (await self.session.execute(statement)).all()]

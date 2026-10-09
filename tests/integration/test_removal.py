import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError

from app.core.errors import InvalidInput, NotFound
from app.models.tables import PendingTransaction, Transaction, TransactionItem, TransactionRemoval
from app.repositories.updates import UpdateRepository
from app.repositories.visualization import VisualizationRepository
from app.services.bot import BotService
from app.services.finance import FinanceService
from app.services.pending import PendingService
from app.services.validation import month_bounds
from tests.integration.test_history import message

pytestmark = pytest.mark.integration


def token(response, label):
    return next(
        b["callback_data"]
        for row in response["reply_markup"]["inline_keyboard"]
        for b in row
        if b["text"] == label
    )


async def save(pending, draft, index):
    p = await pending.create("transaction", draft, index, index, "text")
    await pending.callback(token(await pending.preview(p.id), "Confirm"))
    return p


@pytest.mark.parametrize(
    "kind,command", [("expense", "\\remove expenses"), ("income", "/remove income")]
)
async def test_latest_ten_selection_and_all_aggregates(db, settings, users, draft, kind, command):
    pending = PendingService(db, settings, users[0])
    other = PendingService(db, settings, users[1])
    for i in range(12):
        await save(pending, {**draft, "intent": kind, "description": f"Entry {i:02d}"}, i + 1)
    await save(other, {**draft, "intent": kind, "description": "Private entry"}, 30)
    await save(
        pending,
        {
            **draft,
            "intent": "income" if kind == "expense" else "expense",
            "description": "Other type",
        },
        31,
    )
    budget = await pending.create(
        "budget",
        {"month": month_bounds(None)[0].isoformat(), "category": "food", "amount": 900000},
        32,
        32,
        "command",
    )
    await pending.callback(token(await pending.preview(budget.id), "Confirm"))
    llm = AsyncMock()
    bot = BotService(db, settings, llm, AsyncMock(), AsyncMock(), UpdateRepository(db, settings))
    choices = await bot.process(message(40, command))
    assert len(choices["reply_markup"]["inline_keyboard"]) == 11
    assert "Entry 11" in choices["text"].splitlines()[1]
    assert "Entry 00" not in choices["text"] and "Entry 01" not in choices["text"]
    assert "Private entry" not in choices["text"] and "Other type" not in choices["text"]
    assert await bot.process(message(40, command))  # retry reuses the same snapshot
    # A new entry cannot shift the old buttons to a different transaction.
    await save(pending, {**draft, "intent": kind, "description": "New arrival"}, 41)
    selected = await pending.callback(token(choices, "Remove #1"))
    assert "Entry 11" in selected["text"]
    assert "Edit" not in str(selected["reply_markup"])
    finance = FinanceService(db, users[0])
    before = await finance.summary()
    assert before["expenses" if kind == "expense" else "income"] == 13 * draft["amount"]
    confirm = token(selected, "Confirm")
    results = await asyncio.gather(pending.callback(confirm), pending.callback(confirm))
    assert any("Entry removed" in r["text"] for r in results)
    assert any("Already confirmed" in r["text"] for r in results)
    summary = await finance.summary()
    assert summary["expenses" if kind == "expense" else "income"] == 12 * draft["amount"]
    assert (await finance.current_balance())["balance"] == summary["balance"]
    assert (await finance.advisor_context())["summary"] == summary
    assert (await finance.budgets())[0]["spent"] == summary["expenses"]
    assert all(r["description"] != "Entry 11" for r in await finance.recent(limit=20))
    async with db.transaction() as session:
        groups = await VisualizationRepository(session, users[0]).grouped(*month_bounds(None))
        assert sum(r[3] for r in groups if r[1] == kind) == 12 * draft["amount"]
        assert await session.scalar(select(func.count()).select_from(TransactionRemoval)) == 1
        assert await session.scalar(select(func.count()).select_from(Transaction)) == 15
    assert (await FinanceService(db, users[1]).summary())[
        "expenses" if kind == "expense" else "income"
    ] == draft["amount"]
    repeat = await pending.create(
        "transaction", {**draft, "intent": kind, "amount": 12345}, 45, 45, "text"
    )
    assert not repeat.warnings
    llm.structured.assert_not_awaited()


async def test_ownership_cancel_expiry_stale_and_competing_removals(db, settings, users, draft):
    pending, other = [PendingService(db, settings, u) for u in users]
    await save(pending, draft, 1)
    first = await pending.removal_choices("expense", 2, 2)
    select_token = token(first, "Remove #1")
    with pytest.raises(NotFound):
        await other.callback(select_token)
    selected = await pending.callback(select_token)
    with pytest.raises(NotFound):
        await pending.callback(select_token)
    with pytest.raises(NotFound):
        await other.callback(token(selected, "Confirm"))
    await pending.callback(token(selected, "Cancel"))
    with pytest.raises(NotFound):
        await pending.callback(token(selected, "Confirm"))
    assert len(await FinanceService(db, users[0]).recent()) == 1
    expired = await pending.removal_choices("expense", 3, 3)
    async with db.transaction() as session:
        await session.execute(
            update(PendingTransaction)
            .where(PendingTransaction.source_update_id == 3)
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    with pytest.raises(NotFound):
        await pending.callback(token(expired, "Remove #1"))
    a = await pending.removal_choices("expense", 4, 4)
    b = await pending.removal_choices("expense", 5, 5)
    a = await pending.callback(token(a, "Remove #1"))
    b = await pending.callback(token(b, "Remove #1"))
    await pending.callback(token(a, "Confirm"))
    with pytest.raises(InvalidInput, match="already been removed"):
        await pending.callback(token(b, "Confirm"))
    stale = await pending.existing(5)
    reopened = await pending.preview(stale.id)
    assert "already been removed" in reopened["text"]
    assert "Confirm" not in str(reopened["reply_markup"])
    await pending.callback(token(reopened, "Cancel"))
    assert "No expense entries" in (await pending.removal_choices("expense", 6, 6))["text"]
    replacement = await pending.create("transaction", draft, 7, 7, "text")
    assert not replacement.warnings
    # Removing everything does not reset the opening-balance policy.
    with pytest.raises(InvalidInput, match="before the first"):
        await pending.create(
            "opening", {"amount": 100, "as_of": draft["transaction_date"]}, 8, 8, "command"
        )


async def test_receipt_retained_and_removal_audit_immutable(db, settings, users, draft):
    pending = PendingService(db, settings, users[0])
    receipt = {
        "items": [{"name": "Coffee", "quantity": "1", "unit_price": 25000, "subtotal": 25000}],
        "subtotal": 25000,
        "discount": 0,
        "tax": 0,
        "service_charge": 0,
        "grand_total": 25000,
    }
    await save(pending, {**draft, "receipt": receipt}, 1)
    selected = await pending.callback(
        token(await pending.removal_choices("expense", 2, 2), "Remove #1")
    )
    await pending.callback(token(selected, "Confirm"))
    async with db.transaction() as session:
        assert await session.scalar(select(func.count()).select_from(TransactionItem)) == 1
    for model in (Transaction, TransactionRemoval):
        with pytest.raises(DBAPIError, match="immutable"):
            async with db.transaction() as session:
                await session.execute(update(model).values(user_id=users[1]))


async def test_empty_invalid_commands_and_pending_reopen(db, settings, users, draft):
    llm = AsyncMock()
    bot = BotService(db, settings, llm, AsyncMock(), AsyncMock(), UpdateRepository(db, settings))
    assert "No income entries" in (await bot.process(message(1, "/remove income")))["text"]
    for i, text in enumerate(["/remove", "/remove all", "\\remove expenses 1"], 2):
        with pytest.raises(InvalidInput, match="Use /remove"):
            await bot.process(message(i, text))
    pending = PendingService(db, settings, users[0])
    await save(pending, draft, 5)
    await bot.process(message(6, "/remove expenses"))
    reopened = (await bot.process(message(7, "/pending")))["additional"][0]
    assert "Remove #1" in str(reopened)
    await pending.callback(token(reopened, "Remove #1"))
    reopened = (await bot.process(message(8, "/pending")))["additional"][0]
    assert "Confirm" in str(reopened)
    await pending.callback(token(reopened, "Cancel"))
    llm.structured.assert_not_awaited()

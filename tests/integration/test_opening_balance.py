import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.core.errors import InvalidInput, NotFound
from app.models.tables import OpeningBalance, Transaction
from app.repositories.finance import FinanceRepository
from app.repositories.updates import UpdateRepository
from app.schemas.telegram import Update
from app.services.bot import BotService
from app.services.finance import FinanceService
from app.services.pending import PendingService
from app.services.validation import today

pytestmark = pytest.mark.integration


def button(response, label="Confirm"):
    return next(
        b["callback_data"]
        for b in response["reply_markup"]["inline_keyboard"][0]
        if b["text"] == label
    )


async def opening(service, amount=813794, update_id=1):
    p = await service.create(
        "opening", {"amount": amount, "as_of": str(today())}, update_id, update_id, "text"
    )
    return p, await service.preview(p.id)


async def test_opening_confirmation_isolated_and_not_income(db, settings, users):
    service = PendingService(db, settings, users[0])
    other = PendingService(db, settings, users[1])
    p, preview = await opening(service)
    assert "not income" in preview["text"]
    assert (await FinanceService(db, users[0]).current_balance())["opening"] is None
    with pytest.raises(NotFound):
        await other.callback(button(preview))
    await service.callback(button(preview))
    result = await FinanceService(db, users[0]).current_balance()
    assert result["balance"] == 813794
    assert (await FinanceService(db, users[0]).summary())["income"] == 0
    assert (await FinanceService(db, users[1]).current_balance())["opening"] is None
    async with db.transaction() as session:
        assert await session.scalar(select(func.count()).select_from(Transaction)) == 0


async def test_concurrent_openings_and_duplicate_callback(db, settings, users):
    service = PendingService(db, settings, users[0])
    _, a = await opening(service)
    _, b = await opening(service, 900000, 2)
    results = await asyncio.gather(
        service.callback(button(a)), service.callback(button(b)), return_exceptions=True
    )
    assert sum(isinstance(r, InvalidInput) for r in results) == 1
    async with db.transaction() as session:
        assert await session.scalar(select(func.count()).select_from(OpeningBalance)) == 1
    successful = a if not isinstance(results[0], Exception) else b
    assert "Already confirmed" in (await service.callback(button(successful)))["text"]
    with pytest.raises(InvalidInput):
        await opening(service, 1, 3)


async def test_opening_edit_cancel_zero(db, settings, users):
    service = PendingService(db, settings, users[0])
    p, preview = await opening(service)
    await service.callback(button(preview, "Edit"))
    p = await service.edit(p.id, "amount=0", update_id=2)
    with pytest.raises(NotFound):
        await service.callback(button(preview))
    updated = await service.preview(p.id)
    await service.callback(button(updated, "Cancel"))
    assert (await FinanceService(db, users[0]).current_balance())["opening"] is None
    _, replacement = await opening(service, 0, 3)
    await service.callback(button(replacement))
    assert (await FinanceService(db, users[0]).current_balance())["opening"] == 0


async def test_opening_plus_income_minus_expense_and_month_carry(db, settings, users, draft):
    service = PendingService(db, settings, users[0])
    _, preview = await opening(service)
    await service.callback(button(preview))
    for i, intent, amount in [(2, "income", 500000), (3, "expense", 25000)]:
        p = await service.create(
            "transaction", {**draft, "intent": intent, "amount": amount}, i, i, "text"
        )
        await service.callback(button(await service.preview(p.id)))
    finance = FinanceService(db, users[0])
    assert (await finance.current_balance())["balance"] == 1288794
    assert (await finance.summary())["income"] == 500000
    assert (await finance.summary())["expenses"] == 25000
    async with db.transaction() as session:
        carried = await FinanceRepository(session, users[0]).current_balance(
            today() + timedelta(days=40)
        )
        assert carried["balance"] == 1288794
    with pytest.raises(InvalidInput, match="predates"):
        await service.create(
            "transaction",
            {**draft, "transaction_date": str(today() - timedelta(days=1))},
            5,
            5,
            "text",
        )


async def test_recheck_opening_policy_at_confirmation(db, settings, users, draft):
    service = PendingService(db, settings, users[0])
    _, preview = await opening(service)
    p = await service.create("transaction", draft, 2, 2, "text")
    await service.callback(button(await service.preview(p.id)))
    with pytest.raises(InvalidInput, match="before the first"):
        await service.callback(button(preview))
    with pytest.raises(InvalidInput, match="before the first"):
        await opening(service, update_id=3)


async def test_existing_pending_backdated_transaction_rechecked(db, settings, users, draft):
    service = PendingService(db, settings, users[0])
    p = await service.create(
        "transaction", {**draft, "transaction_date": str(today() - timedelta(days=1))}, 1, 1, "text"
    )
    transaction_preview = await service.preview(p.id)
    _, preview = await opening(service, update_id=2)
    await service.callback(button(preview))
    with pytest.raises(InvalidInput, match="predates"):
        await service.callback(button(transaction_preview))


@pytest.mark.parametrize(
    "text",
    [
        "Saat ini aku memiliki uang sebanyak 813.794 rupiah",
        "Saat ini aku memiliki uang sebanyak 813794",
        "/opening 813794",
    ],
)
async def test_screenshot_text_routes_without_llm(db, settings, users, text):
    llm = AsyncMock()
    bot = BotService(db, settings, llm, AsyncMock(), AsyncMock(), UpdateRepository(db, settings))
    update = Update.model_validate(
        {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": 1,
                "from": {"id": 1001},
                "chat": {"id": 1001, "type": "private"},
                "text": text,
            },
        }
    )
    response = await bot.process(update)
    assert "813,794" in response["text"]
    assert "Pending opening" in response["text"]
    llm.structured.assert_not_awaited()
    balance_update = update.model_copy(deep=True)
    balance_update.update_id = 2
    balance_update.message.message_id = 2
    balance_update.message.text = "/balance"
    assert "No opening balance" in (await bot.process(balance_update))["text"]

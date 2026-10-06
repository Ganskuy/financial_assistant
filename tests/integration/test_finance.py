import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError

from app.core.errors import InvalidInput, NotFound
from app.models.tables import PendingTransaction, Transaction
from app.services.finance import FinanceService
from app.services.pending import PendingService
from app.services.validation import today

pytestmark = pytest.mark.integration


def button(response, action):
    return next(
        b["callback_data"]
        for b in response["reply_markup"]["inline_keyboard"][0]
        if b["text"] == action
    )


async def stage(service, draft, update_id=1):
    pending = await service.create("transaction", draft, update_id, update_id, "text")
    return pending, await service.preview(pending.id)


async def test_no_write_before_confirmation_and_concurrent_confirmation(db, users, settings, draft):
    service = PendingService(db, settings, users[0])
    pending, preview = await stage(service, draft)
    finance = FinanceService(db, users[0])
    assert (await finance.summary())["expenses"] == 0
    token = button(preview, "Confirm")
    assert len(token) < 64 and "25000" not in token
    responses = await asyncio.gather(*(service.callback(token) for _ in range(10)))
    assert sum(r["text"] == "Confirmed and saved." for r in responses) == 1
    async with db.transaction() as session:
        assert await session.scalar(select(func.count()).select_from(Transaction)) == 1
    assert (await finance.summary())["expenses"] == 25000
    assert (await service.existing(1)).id == pending.id


async def test_user_isolation_pending_transactions_budgets_savings(db, users, settings, draft):
    a, b = [PendingService(db, settings, uid) for uid in users]
    pending, preview = await stage(a, draft)
    for action in ("Confirm", "Edit", "Cancel"):
        with pytest.raises(NotFound):
            await b.callback(button(preview, action))
    with pytest.raises(NotFound):
        await b.preview(pending.id)
    with pytest.raises(NotFound):
        await b.edit(pending.id, "amount=100")
    await a.callback(button(preview, "Confirm"))
    for idx, kind, payload in [
        (
            2,
            "budget",
            {"month": today().replace(day=1).isoformat(), "category": "food", "amount": 500000},
        ),
        (
            3,
            "goal",
            {
                "name": "Emergency",
                "target": 1000000,
                "deadline": (today() + timedelta(days=100)).isoformat(),
            },
        ),
        (4, "saving", {"name": "Emergency", "amount": 100000}),
    ]:
        p = await a.create(kind, payload, idx, idx, "command")
        await a.callback(button(await a.preview(p.id), "Confirm"))
    fa, fb = [FinanceService(db, uid) for uid in users]
    assert (await fa.summary())["expenses"] == 25000
    assert (await fa.budgets())[0]["spent"] == 25000
    assert (await fa.savings())[0]["saved"] == 100000
    assert (await fb.summary())["expenses"] == 0
    assert await fb.recent() == []
    assert await fb.budgets() == []
    assert await fb.savings() == []
    malicious_saving = await b.create(
        "saving", {"name": "Emergency", "amount": 1000}, 5, 5, "command"
    )
    with pytest.raises(InvalidInput):
        await b.callback(button(await b.preview(malicious_saving.id), "Confirm"))


async def test_edit_invalidates_old_buttons_and_revalidates(db, users, settings, draft):
    service = PendingService(db, settings, users[0])
    pending, preview = await stage(service, draft)
    await service.callback(button(preview, "Edit"))
    with pytest.raises(NotFound):
        await service.callback(button(preview, "Confirm"))
    for bad in ("user_id=other", "amount=-1", "amount=100\namount=200", "currency=USD"):
        with pytest.raises(InvalidInput):
            await service.edit(pending.id, bad)
    changed = await service.edit(pending.id, "amount=30 ribu\ncategory=other")
    await service.callback(button(await service.preview(changed.id), "Confirm"))
    assert (await FinanceService(db, users[0]).summary())["expenses"] == 30000


async def test_expiration_cancel_and_duplicate_warning(db, users, settings, draft):
    service = PendingService(db, settings, users[0])
    pending, preview = await stage(service, draft)
    async with db.transaction() as session:
        await session.execute(
            update(PendingTransaction)
            .where(PendingTransaction.id == pending.id)
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    with pytest.raises(NotFound):
        await service.callback(button(preview, "Confirm"))
    p2, v2 = await stage(service, draft, 2)
    await service.callback(button(v2, "Cancel"))
    with pytest.raises(NotFound):
        await service.callback(button(v2, "Confirm"))
    _, v3 = await stage(service, draft, 3)
    await service.callback(button(v3, "Confirm"))
    p4, _ = await stage(service, draft, 4)
    assert "similar transaction" in p4.warnings[0]


async def test_income_is_immutable_ledger_balance_and_sql_injection(db, users, settings, draft):
    service = PendingService(db, settings, users[0])
    injection = "'; DROP TABLE transactions; --"
    _, expense = await stage(service, {**draft, "merchant": injection, "description": injection})
    await service.callback(button(expense, "Confirm"))
    _, income = await stage(
        service, {**draft, "intent": "income", "amount": 5000000, "category": "salary"}, 2
    )
    await service.callback(button(income, "Confirm"))
    finance = FinanceService(db, users[0])
    summary = await finance.summary()
    assert summary["income"] == 5000000
    assert summary["expenses"] == 25000
    assert summary["balance"] == 4975000
    assert any(row["description"] == injection for row in await finance.recent())
    with pytest.raises(DBAPIError):
        async with db.transaction() as session:
            await session.execute(
                update(Transaction).where(Transaction.user_id == users[0]).values(amount=1)
            )


async def test_edit_recovery_and_reopening_pending(db, users, settings, draft):
    service = PendingService(db, settings, users[0])
    pending, preview = await stage(service, draft)
    await service.callback(button(preview, "Edit"))
    changed = await service.edit(pending.id, "amount=35000", update_id=99)
    # Re-delivering the correction finds the same pending record after restart.
    assert (await service.existing(99)).id == pending.id
    assert (await service.edit(pending.id, "amount=35000", update_id=99)).version == changed.version
    latest = await service.preview(pending.id)
    await service.callback(button(latest, "Edit"))
    reopened = await service.preview(pending.id)
    await service.callback(button(reopened, "Confirm"))
    assert (await FinanceService(db, users[0]).summary())["expenses"] == 35000

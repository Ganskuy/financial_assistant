import asyncio
from datetime import UTC, datetime

import pytest

from app.core.errors import BudgetExceeded
from app.llm.token_budget import TokenBudget

pytestmark = pytest.mark.integration


async def test_concurrent_hard_limit(db):
    budget = TokenBudget(db)
    results = await asyncio.gather(
        *(budget.reserve(3000) for _ in range(20)), return_exceptions=True
    )
    successful = [r for r in results if not isinstance(r, Exception)]
    assert len(successful) == 6
    assert all(isinstance(r, BudgetExceeded) for r in results if isinstance(r, Exception))
    assert (await budget.usage())["reserved"] == 18000
    await asyncio.gather(*(budget.reconcile(r, 100, 200) for r in successful))
    usage = await budget.usage()
    assert usage["reserved"] == 0 and usage["used"] == 1800
    assert usage["remaining"] == 18200


async def test_reconcile_idempotent(db):
    budget = TokenBudget(db)
    reservation = await budget.reserve(1000)
    await asyncio.gather(*(budget.reconcile(reservation, 100, 100) for _ in range(6)))
    assert (await budget.usage())["used"] == 200
    assert (await budget.usage())["reserved"] == 0


async def test_jakarta_midnight_and_inflight_carry(db):
    clock = [datetime(2026, 10, 5, 16, 59, 59, tzinfo=UTC)]
    budget = TokenBudget(db, clock=lambda: clock[0])
    old = await budget.reserve(12000)
    assert (await budget.usage())["day"] == "2026-10-05"
    clock[0] = datetime(2026, 10, 5, 17, 0, 1, tzinfo=UTC)
    assert (await budget.usage())["day"] == "2026-10-06"
    with pytest.raises(BudgetExceeded):
        await budget.reserve(9000)
    await budget.reconcile(old, 1000, 1000)
    assert (await budget.usage())["used"] == 2000
    assert (await budget.usage())["remaining"] == 18000
    clock[0] = datetime(2026, 10, 6, 17, 0, 1, tzinfo=UTC)
    assert (await budget.usage())["used"] == 0
    assert (await budget.usage())["remaining"] == 20000


async def test_unknown_calls_survive_midnight_and_restart(db):
    clock = datetime(2026, 10, 5, 16, 59, 59, tzinfo=UTC)
    budget = TokenBudget(db, clock=lambda: clock)
    reservation = await budget.reserve(11000)
    await budget.unknown(reservation)
    restarted = TokenBudget(db, clock=lambda: datetime(2026, 10, 7, tzinfo=UTC))
    assert (await restarted.usage())["reserved"] == 11000
    with pytest.raises(BudgetExceeded):
        await restarted.reserve(10000)


async def test_bound_violation_blocks_future_calls(db):
    budget = TokenBudget(db)
    reservation = await budget.reserve(100)
    with pytest.raises(BudgetExceeded):
        await budget.reconcile(reservation, 100, 1)
    with pytest.raises(BudgetExceeded):
        await budget.reserve(1)

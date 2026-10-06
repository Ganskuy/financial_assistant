import json
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event

from app.agents.nodes.finance import FinanceNodes
from app.schemas.finance import Advice
from app.services.advice import compact_advisor_payload, make_facts
from app.services.finance import FinanceService
from app.services.pending import PendingService
from app.services.validation import today
from app.services.visualization import aggregate
from tests.integration.test_finance import button
from tests.integration.test_visualization_workflow import seed

pytestmark = pytest.mark.integration


async def confirm(service, kind, payload, index):
    item = await service.create(kind, payload, index, index, "command")
    await service.callback(button(await service.preview(item.id), "Confirm"))


async def test_advisor_reuses_summary_bounds_goals_and_isolates_users(db, settings, users, draft):
    uid, other = users
    await seed(db, settings, uid, draft, 100, "expense", 25000, "food", today())
    await seed(db, settings, uid, draft, 101, "income", 1000000, "salary", today())
    await seed(db, settings, other, draft, 102, "expense", 999999, "food", today())
    service = PendingService(db, settings, uid)
    await confirm(
        service,
        "budget",
        {"month": str(today().replace(day=1)), "category": "food", "amount": 100000},
        200,
    )
    for i in range(12):
        await confirm(
            service,
            "goal",
            {
                "name": f"Private goal {i}",
                "target": 1000000,
                "deadline": f"{today().year + 1}-12-31",
            },
            300 + i,
        )
    queries = []

    def record(conn, cursor, statement, parameters, context, executemany):
        queries.append(statement)

    event.listen(db.engine.sync_engine, "before_cursor_execute", record)
    try:
        context = await FinanceService(db, uid).advisor_context()
        assert len(queries) == 3
        assert sum("FROM transactions" in q for q in queries) == 1
        assert "LIMIT" in queries[-1]
    finally:
        event.remove(db.engine.sync_engine, "before_cursor_execute", record)
    assert context["summary"]["expenses"] == 25000
    assert context["budgets"][0]["spent"] == context["summary"]["by_category"]["food"]
    assert len(context["savings"]) == 10
    assert len(await FinanceService(db, uid).savings()) == 12
    assert await FinanceService(db, uid).budgets() == context["budgets"]
    assert await FinanceService(db, uid).summary() == context["summary"]
    visual = await aggregate(db, uid, today().strftime("%Y-%m"))
    assert visual["overview"]["net_cashflow"] == context["summary"]["balance"]
    assert visual["overview"]["total_expense"] == context["summary"]["expenses"]
    payload = compact_advisor_payload(
        make_facts(context["summary"], context["budgets"], context["savings"])
    )
    assert "Private goal" not in payload and "999999" not in payload


async def test_large_history_does_not_expand_model_context(db, settings, users, draft):
    uid = users[0]
    llm = AsyncMock()
    llm.structured.return_value = Advice(
        points=[{"action": "protect_surplus", "fact_ids": ["balance"]}]
    )
    nodes = FinanceNodes(db, settings, llm)
    await seed(db, settings, uid, draft, 100, "income", 1000000, "salary", today())
    await seed(db, settings, uid, draft, 101, "expense", 1000, "food", today())
    small = await nodes.gather({"user_id": uid})
    small_payload = compact_advisor_payload(small["facts"])
    for i in range(99):
        await seed(db, settings, uid, draft, 200 + i, "expense", 1000, "food", today())
    for i in range(9):
        await seed(db, settings, uid, draft, 400 + i, "income", 1000000, "salary", today())
    state = await nodes.gather({"user_id": uid})
    await nodes.advise(state)
    llm.structured.assert_awaited_once()
    payload = llm.structured.await_args.args[2]
    values = json.loads(payload.split("\n", 1)[1])
    assert values == {
        "income": 10000000,
        "expenses": 100000,
        "balance": 9900000,
        "category.food": 100000,
    }
    assert len(payload) <= len(small_payload) + 10
    assert draft["description"] not in payload and draft["merchant"] not in payload
    assert "transaction_date" not in payload


async def test_empty_advisor_uses_one_query_and_zero_model_calls(db, settings, users):
    queries = []

    def record(conn, cursor, statement, parameters, context, executemany):
        queries.append(statement)

    event.listen(db.engine.sync_engine, "before_cursor_execute", record)
    llm = AsyncMock()
    nodes = FinanceNodes(db, settings, llm)
    try:
        state = await nodes.gather({"user_id": users[0]})
        response = await nodes.advise(state)
        assert len(queries) == 1
    finally:
        event.remove(db.engine.sync_engine, "before_cursor_execute", record)
    llm.structured.assert_not_awaited()
    assert "No ledger activity" in response["response"]["text"]

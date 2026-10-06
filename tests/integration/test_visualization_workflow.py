import base64
import io
from datetime import timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from app.agents.graphs.workflows import visualization_graph
from app.agents.nodes.visualization import VisualizationNodes
from app.main import create_app
from app.models.tables import ProcessedUpdate, Transaction
from app.repositories.updates import UpdateRepository
from app.schemas.telegram import Update
from app.services.pending import PendingService
from app.services.validation import today
from app.services.visualization import aggregate, default_spec
from app.services.worker import UpdateWorker
from tests.integration.test_workflows import message

pytestmark = pytest.mark.integration


async def seed(db, settings, user, draft, index, kind, amount, category, day):
    service = PendingService(db, settings, user)
    item = await service.create(
        "transaction",
        {
            **draft,
            "intent": kind,
            "amount": amount,
            "category": category,
            "transaction_date": str(day),
        },
        index,
        index,
        "text",
    )
    token = (await service.preview(item.id))["reply_markup"]["inline_keyboard"][0][0][
        "callback_data"
    ]
    await service.callback(token)


async def test_sql_aggregation_isolation_bounds_and_pending_exclusion(db, settings, users, draft):
    user, other = users
    start = today().replace(day=1)
    previous = start - timedelta(days=1)
    entries = [
        (user, "income", 1000000, "salary", start),
        (user, "expense", 20000, "food", start),
        (user, "expense", 30000, "food", start),
        (user, "expense", 10000, "transport", start),
        (user, "expense", 40000, "food", previous),
        (other, "expense", 999999, "food", start),
    ]
    for index, entry in enumerate(entries, 100):
        uid, kind, amount, category, day = entry
        await seed(db, settings, uid, draft, index, kind, amount, category, day)
    await PendingService(db, settings, user).create(
        "transaction", {**draft, "amount": 999}, 200, 200, "text"
    )
    result = await aggregate(db, user, start.strftime("%Y-%m"))
    assert result["overview"]["total_income"] == 1000000
    assert result["overview"]["total_expense"] == 60000
    assert result["overview"]["net_cashflow"] == 940000
    assert result["overview"]["expense_transaction_count"] == 3
    assert result["expense_by_category"][0]["amount"] == 50000
    assert result["comparison"]["previous_month_total_expense"] == 40000
    assert result["comparison"]["expense_change_pct"] == "50.00"
    assert result["comparison"]["income_change_pct"] is None
    other_result = await aggregate(db, other, start.strftime("%Y-%m"))
    assert other_result["overview"]["total_expense"] == 999999
    assert not other_result["comparison"]["previous_period_available"]


@pytest.mark.parametrize("use_analyzer", [False, True])
async def test_webhook_graph_png_delivery_and_retry(db, settings, users, draft, use_analyzer):
    await seed(db, settings, users[0], draft, 100, "expense", 25000, "food", today())
    attempts = []

    def external(request):
        attempts.append(request)
        assert request.url.path.endswith("/sendPhoto")  # no substituted model or separate message
        if len(attempts) == 1:
            return httpx.Response(503, json={"ok": False})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 123}})

    app = create_app(settings, start_workers=False, http_transport=httpx.MockTransport(external))
    async with app.router.lifespan_context(app):
        analyzer = AsyncMock()
        if use_analyzer:
            facts = await aggregate(db, users[0], today().strftime("%Y-%m"))
            analyzer.return_value = default_spec(facts).model_dump_json()
            app.state.bot.visualization = visualization_graph(
                VisualizationNodes(app.state.db, settings, None, analyzer)
            )
        worker = UpdateWorker(app.state.db, app.state.bot, app.state.telegram)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as web:
            body = message(1, f"/visualize {today():%B %Y}")
            headers = {
                "X-Telegram-Bot-Api-Secret-Token": settings.telegram_webhook_secret.get_secret_value()
            }
            assert (
                await web.post("/telegram/webhook", json=body, headers=headers)
            ).status_code == 200
            assert await worker.once()
            async with db.transaction() as session:
                row = await session.get(ProcessedUpdate, 1)
                stored_png = row.response["photo_png"]
                with Image.open(io.BytesIO(base64.b64decode(stored_png))) as image:
                    image.verify()
            assert await worker.once()  # failed delivery, retained response
            async with db.transaction() as session:
                row = await session.get(ProcessedUpdate, 1)
                assert row.status == "responding" and row.response["photo_png"] == stored_png
                row.next_attempt_at -= timedelta(seconds=10)
            assert await worker.once()
            assert len(attempts) == 2
            if use_analyzer:
                analyzer.assert_awaited_once()
            else:
                analyzer.assert_not_awaited()
            assert (
                await web.post("/telegram/webhook", json=body, headers=headers)
            ).status_code == 200
            assert not await worker.once()
            async with db.transaction() as session:
                row = await session.get(ProcessedUpdate, 1)
                assert row.status == "done" and row.response is None
                assert await session.scalar(select(func.count()).select_from(Transaction)) == 1


async def test_invalid_no_data_and_db_retry(db, settings, monkeypatch):
    from app.services.bot import BotService

    updates = UpdateRepository(db, settings)
    llm = AsyncMock()
    bot = BotService(db, settings, llm, AsyncMock(), AsyncMock(), updates)
    for index, command in enumerate(
        ["/visualize", "/visualize BadMonth", "/visualize October 2026 trailing"], 1
    ):
        result = await bot.process(Update.model_validate(message(index, command)))
        assert "Use /visualize" in result["text"]
    result = await bot.process(Update.model_validate(message(4, "/visualize October 2026")))
    assert "No financial data" in result["text"]
    llm.structured.assert_not_awaited()
    parsed = Update.model_validate(message(5, "/visualize October 2026"))
    await updates.enqueue(parsed)
    worker = UpdateWorker(db, bot, AsyncMock())
    monkeypatch.setattr(
        "app.agents.nodes.visualization.aggregate",
        AsyncMock(side_effect=SQLAlchemyError("private SQL")),
    )
    with pytest.raises(SQLAlchemyError):
        await worker.once()
    async with db.transaction() as session:
        row = await session.get(ProcessedUpdate, 5)
        assert row.status == "queued" and row.response is None

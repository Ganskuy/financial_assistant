from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from app.core.errors import TelegramUnavailable
from app.schemas.telegram import Update
from app.services.bot import BotService
from app.telegram.client import TelegramClient


def receipt_update(**media) -> Update:
    return Update.model_validate(
        {
            "update_id": 7,
            "message": {
                "message_id": 11,
                "date": 0,
                "from": {"id": 1001},
                "chat": {"id": 1001, "type": "private"},
                **media,
            },
        }
    )


def make_bot(settings, monkeypatch, telegram=None, existing=None):
    pending = SimpleNamespace(
        existing=AsyncMock(return_value=existing),
        preview=AsyncMock(return_value={"text": "Existing confirmation"}),
    )
    monkeypatch.setattr("app.services.bot.PendingService", lambda *args: pending)
    llm = SimpleNamespace(structured=AsyncMock())
    telegram = telegram or SimpleNamespace(receipt=AsyncMock(return_value=b"normalized-jpeg"))
    updates = SimpleNamespace(user=AsyncMock(return_value=uuid4()))
    bot = BotService(None, settings, llm, None, telegram, updates)
    bot.receipt = SimpleNamespace(
        ainvoke=AsyncMock(return_value={"response": {"text": "New confirmation"}})
    )
    return bot, pending, llm


async def test_largest_available_photo_is_selected_by_pixel_area(settings, monkeypatch):
    bot, _, _ = make_bot(settings, monkeypatch)
    photos = [
        {"file_id": "wide", "file_unique_id": "a", "width": 800, "height": 100, "file_size": 9000},
        {
            "file_id": "largest",
            "file_unique_id": "b",
            "width": 600,
            "height": 800,
            "file_size": 5000,
        },
        {"file_id": "small", "file_unique_id": "c", "width": 100, "height": 100, "file_size": 500},
    ]
    result = await bot.process(receipt_update(photo=photos))
    assert result == {"text": "New confirmation"}
    bot.telegram.receipt.assert_awaited_once_with("largest", 5000, None)
    state = bot.receipt.ainvoke.await_args.args[0]
    assert state["image"] == b"normalized-jpeg"
    assert state["input_source"] == "receipt"
    assert state["update_id"] == 7


@pytest.mark.parametrize("mime", ["image/jpeg", "image/png", "application/octet-stream", None])
async def test_document_mime_and_size_reach_binary_validation(settings, monkeypatch, mime):
    bot, _, _ = make_bot(settings, monkeypatch)
    document = {
        "file_id": "original-file",
        "file_unique_id": "a",
        "file_size": 100,
        "mime_type": mime,
    }
    await bot.process(receipt_update(document=document))
    bot.telegram.receipt.assert_awaited_once_with("original-file", 100, mime)
    bot.receipt.ainvoke.assert_awaited_once()


async def test_get_file_failure_stops_before_graph_and_model(settings, monkeypatch, caplog):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(500, json={"ok": False})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        bot, _, llm = make_bot(settings, monkeypatch, TelegramClient(settings, http))
        with pytest.raises(TelegramUnavailable):
            await bot.process(
                receipt_update(
                    document={"file_id": "file-id", "file_unique_id": "a", "mime_type": "image/png"}
                )
            )
    assert len(requests) == 1
    assert requests[0].url.path.endswith("/getFile")
    bot.receipt.ainvoke.assert_not_awaited()
    llm.structured.assert_not_awaited()
    record = next(record for record in caplog.records if record.msg == "receipt_ingestion_rejected")
    assert record.failure_stage == "ingestion"
    assert record.reason == "download_failed"


@pytest.mark.parametrize("status", ["pending", "confirmed"])
async def test_duplicate_staged_update_skips_download_graph_and_model(
    settings, monkeypatch, status
):
    existing = SimpleNamespace(id=uuid4(), status=status)
    bot, pending, llm = make_bot(settings, monkeypatch, existing=existing)
    result = await bot.process(
        receipt_update(
            document={"file_id": "file-id", "file_unique_id": "a", "mime_type": "image/png"}
        )
    )
    pending.existing.assert_awaited_once_with(7)
    bot.telegram.receipt.assert_not_awaited()
    bot.receipt.ainvoke.assert_not_awaited()
    llm.structured.assert_not_awaited()
    if status == "confirmed":
        assert "already been confirmed" in result["text"]
        pending.preview.assert_not_awaited()
    else:
        assert result == {"text": "Existing confirmation"}
        pending.preview.assert_awaited_once_with(existing.id)

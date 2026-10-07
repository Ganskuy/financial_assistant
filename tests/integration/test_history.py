import time
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from app.core.errors import InvalidInput
from app.repositories.updates import UpdateRepository
from app.schemas.finance import OCR, Extraction
from app.schemas.telegram import Update
from app.services.bot import BotService
from app.services.pending import PendingService
from app.services.validation import today

pytestmark = pytest.mark.integration


def message(update_id, text=None, photo=False, uid=1001):
    data = {
        "message_id": update_id,
        "date": int(time.time()),
        "from": {"id": uid},
        "chat": {"id": uid, "type": "private"},
    }
    if photo:
        data["photo"] = [
            {"file_id": "receipt", "file_unique_id": "receipt", "width": 100, "height": 100}
        ]
    else:
        data["text"] = text
    return Update.model_validate({"update_id": update_id, "message": data})


def confirm_token(preview):
    return next(
        b["callback_data"]
        for b in preview["reply_markup"]["inline_keyboard"][0]
        if b["text"] == "Confirm"
    )


async def test_older_dated_ocr_is_first_after_confirmation_with_pagination(
    db, settings, users, draft
):
    pending = PendingService(db, settings, users[0])
    for index in range(11):
        p = await pending.create(
            "transaction",
            {**draft, "description": f"Text entry {index}"},
            index + 1,
            index + 1,
            "text",
        )
        await pending.callback(confirm_token(await pending.preview(p.id)))

    receipt = {
        **draft,
        "merchant": "FamilyMart",
        "description": "Tteokbokki",
        "transaction_date": (today() - timedelta(days=1)).isoformat(),
        "receipt": {
            "items": [],
            "subtotal": 25000,
            "discount": 0,
            "tax": 0,
            "service_charge": 0,
            "grand_total": 25000,
        },
    }
    llm, telegram = AsyncMock(), AsyncMock()
    telegram.receipt.return_value = b"mock image"
    llm.structured.side_effect = [
        OCR(readable=True, transcription="FamilyMart receipt"),
        Extraction.model_validate({"intent": "expense", "transaction": receipt, "period": None}),
    ]
    bot = BotService(db, settings, llm, AsyncMock(), telegram, UpdateRepository(db, settings))
    preview = await bot.process(message(20, photo=True))
    before = await bot.process(message(21, "/history"))
    assert "FamilyMart" not in before["text"]
    await pending.callback(confirm_token(preview))
    first = (await bot.process(message(22, "/history")))["text"]
    assert "FamilyMart" in first.splitlines()[1]
    assert receipt["transaction_date"] in first.splitlines()[1]
    assert "Next: /history 2" in first
    second = (await bot.process(message(23, "/history 2")))["text"]
    assert "FamilyMart" not in second
    assert "Next:" not in second
    assert "Previous: /history 1" in second
    entries = [
        line for output in [first, second] for line in output.splitlines() if " expense " in line
    ]
    assert len(entries) == len(set(entries)) == 12
    assert "No transactions." in (await bot.process(message(24, "/history", uid=1002)))["text"]
    assert "No transactions on this page." in (await bot.process(message(25, "/history 3")))["text"]
    # A repeat receipt warns, but never removes the confirmed entry from history.
    repeat = await pending.create("transaction", receipt, 26, 26, "receipt")
    assert "similar transaction" in (await pending.preview(repeat.id))["text"]
    assert (await bot.process(message(27, "/history")))["text"] == first
    for index, arg in enumerate(["0", "-1", "10001", "abc", "1 2", "١", "9999999999999"]):
        with pytest.raises(InvalidInput, match="Use /history"):
            await bot.process(message(30 + index, f"/history {arg}"))
    assert llm.structured.await_count == 2  # OCR/extraction only; history uses SQL.

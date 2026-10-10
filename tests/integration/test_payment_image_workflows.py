from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.models.tables import PendingTransaction, Transaction, TransactionItem
from app.repositories.updates import UpdateRepository
from app.schemas.finance import OCR, Extraction
from app.schemas.telegram import Update
from app.services.bot import BotService
from app.services.pending import PendingService

pytestmark = pytest.mark.integration


def image_update(caption=""):
    return Update.model_validate(
        {
            "update_id": 110,
            "message": {
                "message_id": 110,
                "date": 0,
                "from": {"id": 1001},
                "chat": {"id": 1001, "type": "private"},
                "caption": caption,
                "document": {
                    "file_id": "private-image",
                    "file_unique_id": "unique",
                    "mime_type": "image/jpeg",
                },
            },
        }
    )


@pytest.mark.parametrize("caption,intent", [("Pengeluaran", "expense"), ("Pemasukan", "income")])
async def test_payment_confirmation_and_duplicates(db, settings, draft, caption, intent):
    updates = UpdateRepository(db, settings)
    llm = AsyncMock()
    llm.structured.side_effect = [
        OCR(
            transcription="Hasil Transfer\nJumlah Transfer Rp25.000\nTotal Rp25.000", readable=True
        ),
        Extraction(
            intent=intent, period=None, transaction={**draft, "intent": intent, "receipt": None}
        ),
    ]
    telegram = AsyncMock()
    telegram.receipt.return_value = b"normalized"
    bot = BotService(db, settings, llm, AsyncMock(), telegram, updates)
    response = await bot.process(image_update(caption))
    assert "Pending transaction" in response["text"]
    async with db.transaction() as session:
        assert await session.scalar(select(func.count()).select_from(Transaction)) == 0
        assert await session.scalar(select(func.count()).select_from(PendingTransaction)) == 1
    duplicate = await bot.process(image_update(caption))
    assert "Pending transaction" in duplicate["text"]
    assert llm.structured.await_count == 2
    telegram.receipt.assert_awaited_once()
    user = await updates.user(1001)
    pending = PendingService(db, settings, user)
    token = duplicate["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    await pending.callback(token)
    await pending.callback(token)
    async with db.transaction() as session:
        rows = (await session.scalars(select(Transaction))).all()
        assert len(rows) == 1 and rows[0].type == intent and rows[0].amount == 25000
        assert rows[0].input_source == "receipt"
        assert await session.scalar(select(func.count()).select_from(TransactionItem)) == 0


async def test_unidentified_transfer_direction_never_stages(db, settings):
    llm = AsyncMock()
    llm.structured.return_value = OCR(
        transcription="Hasil Transfer\nJumlah Total Rp40.000", readable=True
    )
    telegram = AsyncMock()
    telegram.receipt.return_value = b"normalized"
    bot = BotService(db, settings, llm, AsyncMock(), telegram, UpdateRepository(db, settings))
    response = await bot.process(image_update())
    assert "sent or received" in response["text"]
    assert llm.structured.await_count == 1
    async with db.transaction() as session:
        assert await session.scalar(select(func.count()).select_from(PendingTransaction)) == 0
        assert await session.scalar(select(func.count()).select_from(Transaction)) == 0

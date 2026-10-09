import hmac
import json
import logging
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from app.core.errors import InvalidInput, TelegramUnavailable
from app.core.logging import request_id, user_hash
from app.models.tables import DailyLLMUsage, ProcessedUpdate
from app.schemas.telegram import Update

router = APIRouter()
log = logging.getLogger("finance")


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/ready")
async def ready(request: Request):
    try:
        async with request.app.state.db.transaction() as session:
            # Check DB connectivity, schema and migration head. No external model calls.
            await session.execute(select(DailyLLMUsage.usage_date).limit(1))
            await session.execute(select(ProcessedUpdate.next_attempt_at).limit(1))
            version = await session.scalar(text("SELECT version_num FROM alembic_version"))
            if version != "0004":
                raise HTTPException(503, "Database migration mismatch")
        worker = request.app.state.worker
        if worker is not None and any(task.done() for task in worker.tasks):
            raise HTTPException(503, "Worker unavailable")
    except SQLAlchemyError as exc:
        raise HTTPException(503, "Database unavailable") from exc
    return {"status": "ready"}


@router.post("/telegram/webhook")
async def webhook(request: Request):
    settings = request.app.state.settings
    provided = request.headers.get("x-telegram-bot-api-secret-token", "")
    if not hmac.compare_digest(
        provided.encode(), settings.telegram_webhook_secret.get_secret_value().encode()
    ):
        raise HTTPException(403, "Forbidden")
    if request.headers.get("content-type", "").split(";")[0] != "application/json":
        raise HTTPException(415, "JSON required")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 65536:
            raise HTTPException(413, "Update too large")
    try:
        raw = json.loads(body)
        if not isinstance(raw, dict):
            raise ValueError("Invalid update")
        update = Update.model_validate(raw)
        if (update.message is None) == (update.callback_query is None):
            return {"ok": True, "ignored": True}
        user_id, chat_id, _ = update.identity()
    except (ValueError, TypeError, ValidationError) as exc:
        raise HTTPException(400, "Malformed Telegram update") from exc
    message = update.message or update.callback_query.message
    sender = update.message.from_user if update.message else update.callback_query.from_user
    if (
        message.chat.type != "private"
        or chat_id != user_id
        or sender.is_bot
        or user_id not in settings.allowed_users
    ):
        raise HTTPException(403, "Forbidden")
    if update.message and (
        time.time() - message.date > settings.max_update_age_hours * 3600
        or message.date > time.time() + 60
    ):
        return {"ok": True, "ignored": True}
    token = request_id.set(f"tg-{update.update_id}")
    try:
        accepted = await request.app.state.updates.enqueue(update)
        if update.callback_query and accepted:
            try:
                await request.app.state.telegram.call(
                    "answerCallbackQuery",
                    {"callback_query_id": update.callback_query.id, "text": "Processing"},
                )
            except TelegramUnavailable:
                pass
        log.info(
            "webhook_accepted",
            extra={
                "update_id": update.update_id,
                "user_hash": user_hash(user_id, settings.app_secret_key.get_secret_value()),
                "status": "queued" if accepted else "duplicate",
            },
        )
        return {"ok": True}
    except InvalidInput as exc:
        raise HTTPException(429, "Rate limit reached", headers={"Retry-After": "60"}) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(503, "Database unavailable") from exc
    finally:
        request_id.reset(token)

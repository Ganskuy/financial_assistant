import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import exists, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import aliased

from app.core.errors import SafeError, TelegramUnavailable
from app.core.logging import request_id
from app.models.tables import ProcessedUpdate
from app.schemas.telegram import Update

log = logging.getLogger("finance")


class UpdateWorker:
    def __init__(self, db, bot, telegram):
        self.db, self.bot, self.telegram = db, bot, telegram
        self.stopping = asyncio.Event()
        self.tasks: list[asyncio.Task] = []

    def start(self, count: int) -> None:
        self.tasks = [
            asyncio.create_task(self.run(), name=f"telegram-worker-{i}") for i in range(count)
        ]

    async def close(self) -> None:
        self.stopping.set()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def run(self) -> None:
        while not self.stopping.is_set():
            try:
                processed = await self.once()
                if not processed:
                    await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("worker_failure", extra={"error_type": type(exc).__name__})
                await asyncio.sleep(2)

    async def once(self) -> bool:
        async with self.db.transaction() as session:
            earlier = aliased(ProcessedUpdate)
            row = await session.scalar(
                select(ProcessedUpdate)
                .where(
                    ProcessedUpdate.status.in_(["queued", "responding"]),
                    ProcessedUpdate.next_attempt_at <= datetime.now(UTC),
                    ~exists(
                        select(earlier.update_id).where(
                            earlier.telegram_user_id == ProcessedUpdate.telegram_user_id,
                            earlier.update_id < ProcessedUpdate.update_id,
                            earlier.status.in_(["queued", "responding"]),
                        )
                    ),
                )
                .order_by(ProcessedUpdate.update_id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if row is None:
                return False
            locked = await session.scalar(
                select(func.pg_try_advisory_xact_lock(32, row.telegram_user_id % 2147483647))
            )
            if not locked:
                return False
            context_token = request_id.set(f"tg-{row.update_id}")
            try:
                if row.status == "queued":
                    try:
                        async with asyncio.timeout(450):
                            response = await self.bot.process(Update.model_validate(row.payload))
                    except SafeError as exc:
                        response = {"text": str(exc)}
                    except TelegramUnavailable:
                        response = {
                            "text": "Receipt download failed. Please resend the image later. No record was written."
                        }
                    except TimeoutError:
                        response = {
                            "text": "Processing timed out. Use /pending to check for an existing preview before resubmitting."
                        }
                    except SQLAlchemyError:
                        # Roll back the job claim; a database outage must not lose work.
                        raise
                    except Exception as exc:
                        log.error(
                            "workflow_failure",
                            extra={"error_type": type(exc).__name__, "update_id": row.update_id},
                        )
                        response = {
                            "text": "Processing failed safely. Check /pending and /history before resubmitting."
                        }
                    # Response is committed before delivery. Sending retries never repeat model calls.
                    row.response, row.status, row.payload = response, "responding", {}
                else:
                    try:
                        await self.telegram.send(row.chat_id, row.response)
                        for extra in row.response.get("additional", []):
                            await self.telegram.send(row.chat_id, extra)
                    except TelegramUnavailable:
                        row.attempts += 1
                        row.next_attempt_at = datetime.now(UTC) + timedelta(
                            seconds=min(300, 2**row.attempts)
                        )
                        if row.attempts >= 8:
                            row.status = "failed"
                        log.warning(
                            "telegram_delivery_failure",
                            extra={"update_id": row.update_id, "status": row.status},
                        )
                    else:
                        row.status, row.payload, row.response = "done", {}, None
                        row.finished_at = datetime.now(UTC)
                log.info(
                    "update_processed", extra={"update_id": row.update_id, "status": row.status}
                )
            finally:
                request_id.reset(context_token)
        return True

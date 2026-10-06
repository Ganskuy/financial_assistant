import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert

from app.core.config import Settings
from app.core.db import Database
from app.core.errors import InvalidInput
from app.models.tables import ProcessedUpdate, RateBucket, User
from app.schemas.telegram import Update


class UpdateRepository:
    def __init__(self, db: Database, settings: Settings):
        self.db, self.settings = db, settings

    async def enqueue(self, update: Update) -> bool:
        user_id, chat_id, message_id = update.identity()
        event_key = (
            "callback:" + update.callback_query.id
            if update.callback_query
            else f"message:{message_id}"
        )
        duplicate_query = select(ProcessedUpdate.update_id).where(
            or_(
                ProcessedUpdate.update_id == update.update_id,
                (ProcessedUpdate.telegram_user_id == user_id)
                & (ProcessedUpdate.event_key == event_key),
            )
        )
        async with self.db.transaction() as session:
            if await session.scalar(duplicate_query) is not None:
                return False
            # Locks only the rate limiter namespace; worker uses a separate namespace.
            await session.execute(select(func.pg_advisory_xact_lock(31, user_id % 2147483647)))
            if await session.scalar(duplicate_query) is not None:
                return False
            minute = int(time.time()) // 60
            await session.execute(
                insert(RateBucket)
                .values(telegram_user_id=user_id, minute=minute, count=0)
                .on_conflict_do_nothing()
            )
            bucket = await session.scalar(
                select(RateBucket).where(RateBucket.telegram_user_id == user_id).with_for_update()
            )
            if bucket.minute != minute:
                bucket.minute, bucket.count = minute, 0
            if bucket.count >= self.settings.rate_limit_per_minute:
                raise InvalidInput("Too many requests. Please wait a minute.")
            bucket.count += 1
            # Only validated supported fields are retained; photos are just opaque Telegram IDs.
            session.add(
                ProcessedUpdate(
                    update_id=update.update_id,
                    telegram_user_id=user_id,
                    chat_id=chat_id,
                    message_id=message_id,
                    event_key=event_key,
                    payload=update.model_dump(mode="json", by_alias=True, exclude_none=True),
                )
            )
            return True

    async def user(self, telegram_user_id: int):
        async with self.db.transaction() as session:
            await session.execute(
                insert(User).values(telegram_user_id=telegram_user_id).on_conflict_do_nothing()
            )
            return await session.scalar(
                select(User.id).where(User.telegram_user_id == telegram_user_id)
            )

    async def recoverable_count(self) -> int:
        async with self.db.transaction() as session:
            return int(
                await session.scalar(
                    select(func.count())
                    .select_from(ProcessedUpdate)
                    .where(ProcessedUpdate.status.in_(["queued", "responding"]))
                )
            )

    async def prune(self) -> None:
        # Permanent idempotency tombstones remain; sensitive update payloads do not.
        from sqlalchemy import update

        async with self.db.transaction() as session:
            await session.execute(
                update(ProcessedUpdate)
                .where(
                    ProcessedUpdate.received_at < datetime.now(UTC) - timedelta(days=2),
                    ProcessedUpdate.status == "failed",
                )
                .values(payload={}, response=None)
            )

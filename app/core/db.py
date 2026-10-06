import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings


class Database:
    def __init__(self, settings: Settings):
        self.engine = create_async_engine(
            settings.database_url.get_secret_value(),
            pool_pre_ping=True,
            pool_size=8,
            max_overflow=4,
            pool_timeout=10,
            echo=False,
            connect_args={
                "timeout": 10,
                "command_timeout": 15,
                "server_settings": {"application_name": "financial_assistant"},
            },
        )
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

        @event.listens_for(self.engine.sync_engine, "before_cursor_execute")
        def before(conn, cursor, statement, parameters, context, executemany):
            context.started_at = time.monotonic()

        @event.listens_for(self.engine.sync_engine, "after_cursor_execute")
        def after(conn, cursor, statement, parameters, context, executemany):
            logging.getLogger("finance").debug(
                "db_query",
                extra={
                    "latency_ms": round((time.monotonic() - context.started_at) * 1000),
                    "status": "ok",
                },
            )

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        async with self.sessions() as session, session.begin():
            yield session

    async def close(self) -> None:
        await self.engine.dispose()

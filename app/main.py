from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from app.api.routes import router
from app.core.config import Settings, get_settings
from app.core.db import Database
from app.core.logging import setup_logging
from app.llm.client import OpenRouterClient
from app.llm.token_budget import TokenBudget
from app.repositories.updates import UpdateRepository
from app.services.bot import BotService
from app.services.worker import UpdateWorker
from app.telegram.client import TelegramClient


def create_app(
    settings: Settings | None = None, start_workers: bool = True, http_transport=None
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        configured = settings or get_settings()
        setup_logging()
        db = Database(configured)
        # Disable proxy env inheritance, redirects, HTTP logging and network retries.
        async with httpx.AsyncClient(
            follow_redirects=False,
            trust_env=False,
            transport=http_transport,
            limits=httpx.Limits(max_connections=12, max_keepalive_connections=8),
        ) as http:
            telegram = TelegramClient(configured, http)
            budget = TokenBudget(db, configured.daily_llm_token_limit)
            llm = OpenRouterClient(configured, budget, http)
            updates = UpdateRepository(db, configured)
            bot = BotService(db, configured, llm, budget, telegram, updates)
            worker = UpdateWorker(db, bot, telegram) if start_workers else None
            for name, value in {
                "settings": configured,
                "db": db,
                "telegram": telegram,
                "budget": budget,
                "updates": updates,
                "bot": bot,
                "worker": worker,
            }.items():
                setattr(app.state, name, value)
            if worker:
                worker.start(configured.worker_count)
            try:
                yield
            finally:
                if worker:
                    await worker.close()
                await db.close()

    app = FastAPI(
        title="Telegram Financial Assistant",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.include_router(router)
    return app


app = create_app()

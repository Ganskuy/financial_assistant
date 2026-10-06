"""Read safe operational counts or prune expired delivery payloads; never deletes ledger data."""

import argparse
import asyncio
import json

from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.db import Database
from app.llm.token_budget import TokenBudget
from app.models.tables import LLMReservation, ProcessedUpdate
from app.repositories.updates import UpdateRepository


async def main(command: str) -> None:
    settings = get_settings()
    db = Database(settings)
    try:
        if command == "prune":
            await UpdateRepository(db, settings).prune()
            print(
                "Cleared failed delivery payloads older than two days; idempotency tombstones and ledger retained."
            )
            return
        async with db.transaction() as session:
            jobs = dict(
                (
                    await session.execute(
                        select(ProcessedUpdate.status, func.count()).group_by(
                            ProcessedUpdate.status
                        )
                    )
                ).all()
            )
            uncertain = int(
                await session.scalar(
                    select(func.count())
                    .select_from(LLMReservation)
                    .where(LLMReservation.status.in_(["reserved", "unknown"]))
                )
            )
        print(
            json.dumps(
                {
                    "jobs": jobs,
                    "unresolved_calls": uncertain,
                    "usage": await TokenBudget(db, settings.daily_llm_token_limit).usage(),
                }
            )
        )
    finally:
        await db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["status", "prune"])
    asyncio.run(main(parser.parse_args().command))

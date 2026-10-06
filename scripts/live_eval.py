"""Opt-in extraction evaluation; uses the same global PostgreSQL token budget."""

import argparse
import asyncio
import json
from pathlib import Path

import httpx

from app.agents.prompts.system import EXTRACTION_PROMPT
from app.core.config import get_settings
from app.core.db import Database
from app.core.errors import SafeError
from app.llm.client import OpenRouterClient
from app.llm.token_budget import TokenBudget
from app.schemas.finance import Extraction
from app.services.validation import today


async def main() -> None:
    settings = get_settings()
    db = Database(settings)
    cases = json.loads(await asyncio.to_thread(Path("tests/evals/cases.json").read_text))
    passed = attempted = 0
    try:
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as http:
            llm = OpenRouterClient(settings, TokenBudget(db, settings.daily_llm_token_limit), http)
            for case in cases:
                if case["kind"] == "advisor_grounding":
                    continue
                attempted += 1
                try:
                    result = await llm.structured(
                        "extraction",
                        EXTRACTION_PROMPT + f" Today in Asia/Jakarta: {today()}.",
                        json.dumps({"untrusted_text": case["input"]}),
                        Extraction,
                    )
                    match = result.intent == case["intent"] and (
                        case["amount"] is None
                        or (
                            result.transaction is not None
                            and result.transaction.amount == case["amount"]
                        )
                    )
                except SafeError as exc:
                    print(f"{case['kind']}: blocked/failed ({type(exc).__name__})")
                    break
                passed += int(match)
                print(f"{case['kind']}: {'PASS' if match else 'FAIL'}")
        print(f"Exact intent/amount matches: {passed}/{attempted}. No financial writes performed.")
        if passed != attempted or attempted != len(cases) - 1:
            raise SystemExit(1)
    finally:
        await db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live", action="store_true", required=True, help="Authorize real, billed OpenRouter calls"
    )
    parser.parse_args()
    asyncio.run(main())

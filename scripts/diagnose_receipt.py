"""Live OCR/extraction diagnostic. Uses token accounting, never stages financial writes.

Run: python -m scripts.diagnose_receipt /path/to/receipt.jpeg --runs 3
"""

import argparse
import asyncio
import json
import re
from pathlib import Path

import httpx

from app.agents.nodes.finance import FinanceNodes
from app.core.config import get_settings
from app.core.db import Database
from app.core.errors import SafeError
from app.core.logging import request_id, setup_logging
from app.llm.client import OpenRouterClient
from app.llm.token_budget import TokenBudget
from app.services.validation import today
from app.telegram.client import normalize_image


class DiagnosticClient(OpenRouterClient):
    async def _request(self, payload: dict, role: str, amount: int) -> dict:
        raw = await super()._request(payload, role, amount)
        if role == "extraction":
            try:
                content = json.loads(raw["choices"][0]["message"]["content"])
                value = content["transaction"]["transaction_date"]
                # Only print a date-shaped value, never arbitrary model text.
                safe = (
                    value
                    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)
                    else "non-ISO-date"
                )
                print(
                    json.dumps({"raw_model_date": safe, "raw_type": type(value).__name__}),
                    flush=True,
                )
            except (KeyError, IndexError, TypeError, ValueError):
                print(json.dumps({"raw_model_date": "unavailable"}), flush=True)
        return raw


async def diagnose(path: Path, runs: int) -> bool:
    settings = get_settings()
    if (
        await asyncio.to_thread(path.stat)
    ).st_size > settings.max_receipt_file_size_mb * 1024 * 1024:
        raise ValueError("Image exceeds configured receipt size limit")
    image = await asyncio.to_thread(normalize_image, await asyncio.to_thread(path.read_bytes))
    setup_logging()
    db = Database(settings)
    passed = True
    try:
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as http:
            budget = TokenBudget(db, settings.daily_llm_token_limit)
            nodes = FinanceNodes(db, settings, DiagnosticClient(settings, budget, http))
            before = (await budget.usage())["used"]
            for run in range(1, runs + 1):
                token = request_id.set(f"receipt-diagnostic-{run}")
                stage = "ocr"
                try:
                    state = {"image": image, "input_source": "receipt"}
                    state.update(await nodes.ocr(state))
                    stage = "extraction"
                    state.update(await nodes.extract(state))
                    stage = "validation"
                    await nodes.validate(state)
                    print(
                        json.dumps({"run": run, "result": "pass", "today": str(today())}),
                        flush=True,
                    )
                except SafeError as exc:
                    passed = False
                    print(
                        json.dumps({"run": run, "stage": stage, "error_type": type(exc).__name__}),
                        flush=True,
                    )
                    break
                finally:
                    request_id.reset(token)
            after = (await budget.usage())["used"]
            print(json.dumps({"budget_token_delta": after - before}), flush=True)
    finally:
        await db.close()
    return passed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("--runs", type=int, choices=range(1, 6), default=1)
    args = parser.parse_args()
    try:
        passed = asyncio.run(diagnose(args.image, args.runs))
    except Exception as exc:
        # Provider/connection exception bodies can contain secrets; emit only the type.
        print(json.dumps({"diagnostic_error": type(exc).__name__}), flush=True)
        raise SystemExit(1) from None
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()

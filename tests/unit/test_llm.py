import json
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from app.core.errors import BudgetExceeded, InvalidModelOutput, ModelUnavailable
from app.llm.client import OpenRouterClient, reservation_bound, strict_json_schema
from app.schemas.finance import OCR, Extraction


@pytest.fixture
def budget():
    mock = AsyncMock()
    mock.reserve.return_value = uuid4()
    mock.usage.return_value = {"remaining": 19000}
    return mock


def successful(content=None):
    return {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "content": json.dumps(
                        content or {"transcription": "Coffee 25000", "readable": True}
                    )
                },
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
    }


async def test_accounting_and_model_api(settings, budget):
    calls = []

    async def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        assert payload["provider"]["allow_fallbacks"] is False
        assert payload["response_format"]["json_schema"]["strict"] is True
        return httpx.Response(200, json=successful())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await OpenRouterClient(settings, budget, http).structured(
            "vision", "OCR", "data", OCR
        )
    assert result.readable
    assert len(calls) == 1
    budget.reconcile.assert_awaited_once_with(budget.reserve.return_value, 100, 20)


async def test_budget_denial_never_calls_network(settings, budget):
    budget.reserve.side_effect = BudgetExceeded()
    handler = AsyncMock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(BudgetExceeded):
            await OpenRouterClient(settings, budget, http).structured("vision", "OCR", "data", OCR)
    handler.assert_not_awaited()


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
async def test_permanent_errors_do_not_retry(settings, budget, status):
    handler = AsyncMock(
        return_value=httpx.Response(status, json={"error": "private provider text"})
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(ModelUnavailable):
            await OpenRouterClient(settings, budget, http).structured("vision", "OCR", "data", OCR)
    assert handler.await_count == 1
    budget.unknown.assert_awaited_once()


@pytest.mark.parametrize("failure", [429, 500, "timeout"])
async def test_transient_retry_gets_fresh_reservation(settings, budget, monkeypatch, failure):
    monkeypatch.setattr("app.llm.client.asyncio.sleep", AsyncMock())
    count = 0

    async def handler(request):
        nonlocal count
        count += 1
        if count == 1:
            if failure == "timeout":
                raise httpx.ReadTimeout("private URL")
            return httpx.Response(failure, headers={"Retry-After": "1"})
        return httpx.Response(200, json=successful())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await OpenRouterClient(settings, budget, http).structured("vision", "OCR", "data", OCR)
    assert budget.reserve.await_count == 2
    assert budget.unknown.await_count == 1
    assert budget.reconcile.await_count == 1


async def test_missing_usage_never_refunds_or_retries(settings, budget):
    handler = AsyncMock(return_value=httpx.Response(200, json={"choices": []}))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(InvalidModelOutput):
            await OpenRouterClient(settings, budget, http).structured("vision", "OCR", "data", OCR)
    budget.unknown.assert_awaited_once()
    budget.reconcile.assert_not_awaited()
    assert handler.await_count == 1


async def test_schema_failure_is_accounted_not_retried(settings, budget):
    handler = AsyncMock(return_value=httpx.Response(200, json=successful({"unexpected": "value"})))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(InvalidModelOutput):
            await OpenRouterClient(settings, budget, http).structured("vision", "OCR", "data", OCR)
    budget.reconcile.assert_awaited_once()
    assert handler.await_count == 1


def test_schema_and_reservation_include_full_input():
    schema = strict_json_schema(Extraction)
    assert schema["additionalProperties"] is False
    for definition in schema["$defs"].values():
        if definition.get("type") == "object":
            assert set(definition["required"]) == set(definition["properties"])
    payload = {
        "messages": [{"role": "user", "content": "🙂" * 100}],
        "max_tokens": 500,
        "schema": schema,
    }
    assert reservation_bound(payload, 0) > 4096 + 500 + 400

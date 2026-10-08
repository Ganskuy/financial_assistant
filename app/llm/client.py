import asyncio
import base64
import json
import logging
import math
import random
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TypeVar

import httpx
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableLambda
from langsmith import tracing_context
from pydantic import BaseModel, ValidationError

from app.core.config import Settings
from app.core.errors import InvalidModelOutput, ModelUnavailable
from app.llm.router import Role, profile
from app.llm.token_budget import TokenBudget

T = TypeVar("T", bound=BaseModel)
log = logging.getLogger("finance")


def retry_delay(value: str) -> float:
    """Honor seconds or HTTP dates; long delays fail safely instead of retrying early."""
    try:
        delay = float(value)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
            delay = (parsed - datetime.now(UTC)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return 0.0
    return max(0.0, delay) if math.isfinite(delay) else 0.0


def strict_json_schema(schema: type[BaseModel]) -> dict:
    result = schema.model_json_schema()

    def visit(node):
        if isinstance(node, dict):
            node.pop("default", None)
            if node.get("type") == "object":
                node["additionalProperties"] = False
                node["required"] = list(node.get("properties", {}))
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(result)
    return result


def reservation_bound(payload: dict, image_allowance: int) -> int:
    # Every UTF-8 byte can conservatively be a text token for reviewed byte-based
    # tokenizers. Include full JSON schema, message framing, and 4096 overhead tokens.
    # Image base64 is NOT textual input: replace it with its certified patch envelope.
    copy = json.loads(json.dumps(payload))
    image_count = 0
    for message in copy["messages"]:
        if isinstance(message["content"], list):
            for part in message["content"]:
                if part.get("type") == "image_url":
                    part["image_url"]["url"] = "bounded-private-image"
                    image_count += 1
    if image_count > 1 or (image_count and not image_allowance):
        raise ValueError("Unsupported image envelope")
    size = len(json.dumps(copy, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    return size + 4096 + image_count * image_allowance + payload["max_tokens"]


class OpenRouterClient:
    def __init__(self, settings: Settings, budget: TokenBudget, http: httpx.AsyncClient):
        self.settings, self.budget, self.http = settings, budget, http
        for role in ("vision", "extraction", "advisor"):
            profile(settings, role)

    async def structured(
        self, role: Role, system: str, data: str, schema: type[T], image: bytes | None = None
    ) -> T:
        # 6000 source characters can expand sixfold when JSON-escaped. The source
        # limit is enforced by the extraction boundary; include the JSON envelope.
        if len(data) > (36_100 if role == "extraction" else 6000):
            raise InvalidModelOutput("input_too_long", retryable=False)
        selected = profile(self.settings, role)
        # LangChain messages and Runnable wrap the only permitted model transport.
        content: str | list = data
        if image is not None:
            if role != "vision":
                raise InvalidModelOutput("unexpected_image", retryable=False)
            content = [
                {"type": "text", "text": data},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/jpeg;base64," + base64.b64encode(image).decode(),
                        "detail": "high",
                    },
                },
            ]
        messages = [SystemMessage(content=system), HumanMessage(content=content)]
        payload = {
            "model": selected.model,
            "messages": [
                {"role": "system" if isinstance(m, SystemMessage) else "user", "content": m.content}
                for m in messages
            ],
            "max_tokens": selected.max_output,
            "stream": False,
            "provider": {
                "require_parameters": True,
                "allow_fallbacks": False,
                "data_collection": "deny",
            },
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "strict": True,
                    "schema": strict_json_schema(schema),
                },
            },
        }
        if role == "advisor":
            payload["reasoning"] = {"effort": "low"}
        amount = reservation_bound(payload, selected.image_allowance)

        async def invoke(body):
            return await self._request(body, role, amount)

        with tracing_context(enabled=False):
            raw = await RunnableLambda(invoke).ainvoke(payload)
        try:
            choice = raw["choices"][0]
            if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
                raise ValueError("Invalid message shape")
            if choice["message"].get("refusal") or choice.get("finish_reason") == "content_filter":
                raise InvalidModelOutput("refusal", retryable=False)
            if choice.get("finish_reason") != "stop" or choice["message"].get("tool_calls"):
                raise ValueError("Incomplete or unexpected response")
            value = choice["message"]["content"]
            if not isinstance(value, str) or not value.strip():
                raise ValueError("Empty response")
            return schema.model_validate_json(value)
        except (KeyError, IndexError, TypeError, ValueError, ValidationError) as exc:
            log.warning(
                "structured_output_rejected", extra={"role": role, "reason": "schema_or_incomplete"}
            )
            raise InvalidModelOutput("schema_or_incomplete") from exc

    async def _request(self, payload: dict, role: Role, amount: int) -> dict:
        for attempt in range(self.settings.openrouter_max_attempts):
            reservation = await self.budget.reserve(amount)
            started = time.monotonic()
            transient = False
            retry_after = 0.0
            try:
                # Total deadline, not just per-chunk HTTP read timeout.
                async with asyncio.timeout(self.settings.openrouter_timeout_seconds):
                    response = await self.http.post(
                        self.settings.openrouter_base_url + "/chat/completions",
                        headers={
                            "Authorization": "Bearer "
                            + self.settings.openrouter_api_key.get_secret_value(),
                            "X-OpenRouter-Title": "Personal Finance Assistant",
                        },
                        json=payload,
                        timeout=self.settings.openrouter_timeout_seconds,
                    )
                if response.status_code != 200:
                    transient = response.status_code in {408, 429} or response.status_code >= 500
                    retry_after = retry_delay(response.headers.get("retry-after", "0"))
                    await self.budget.unknown(reservation)
                    if not transient or retry_after > 5:
                        log.warning(
                            "llm_failure",
                            extra={
                                "role": role,
                                "status": "failed",
                                "attempt": attempt + 1,
                                "reason": "retry_later" if retry_after > 5 else "http_permanent",
                            },
                        )
                        raise ModelUnavailable()
                else:
                    try:
                        raw = response.json()
                        usage = raw["usage"]
                        inp, out = usage["prompt_tokens"], usage["completion_tokens"]
                        if (
                            type(inp) is not int
                            or type(out) is not int
                            or inp < 0
                            or out < 0
                            or type(usage["total_tokens"]) is not int
                            or usage["total_tokens"] != inp + out
                        ):
                            raise ValueError("Invalid usage metadata")
                    except (ValueError, KeyError, TypeError) as exc:
                        await self.budget.unknown(reservation)
                        raise InvalidModelOutput("invalid_usage", retryable=False) from exc
                    await self.budget.reconcile(reservation, inp, out)
                    log.info(
                        "llm_call",
                        extra={
                            "role": role,
                            "model": payload["model"],
                            "input_tokens": inp,
                            "output_tokens": out,
                            "total_tokens": inp + out,
                            "latency_ms": round((time.monotonic() - started) * 1000),
                            "status": "ok",
                            "prompt_version": "extraction-v3"
                            if role == "extraction"
                            else "finance-v1",
                            "cost": usage.get("cost"),
                            "remaining_tokens": (await self.budget.usage())["remaining"],
                        },
                    )
                    return raw
            except (
                httpx.TimeoutException,
                httpx.NetworkError,
                httpx.RemoteProtocolError,
                TimeoutError,
            ):
                # Provider may still be generating. Never release this reservation.
                await self.budget.unknown(reservation)
                transient = True
            except asyncio.CancelledError:
                # An un-settled reservation already remains durable after process shutdown.
                raise
            log.warning(
                "llm_failure",
                extra={
                    "role": role,
                    "model": payload["model"],
                    "status": "transient" if transient else "failed",
                    "attempt": attempt + 1,
                },
            )
            if not transient or attempt + 1 == self.settings.openrouter_max_attempts:
                break
            await asyncio.sleep(max(retry_after, (2**attempt) + random.uniform(0, 0.5)))
        raise ModelUnavailable()

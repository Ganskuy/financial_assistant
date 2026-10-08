"""Opt-in paired extraction evaluation; token accounting only, no financial writes.

Without --live, validate the labeled dataset without loading credentials or calling a model.
The baseline freezes historical prompts, OCR/extraction methods and image normalization;
both variants intentionally share the installed transport, schemas and business validator.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import hashlib
import io
import json
import logging
import math
import re
import resource
import subprocess
import sys
import time
import warnings
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, TypeVar

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel

from app.agents.nodes.finance import FinanceNodes
from app.agents.prompts.system import EXTRACTION_PROMPT, OCR_PROMPT
from app.core.config import get_settings
from app.core.db import Database
from app.core.errors import BudgetExceeded, InvalidInput, InvalidModelOutput, ModelUnavailable
from app.core.logging import request_id
from app.llm.client import OpenRouterClient
from app.llm.router import Role
from app.llm.token_budget import TokenBudget
from app.schemas.finance import OCR, Extraction
from app.services.receipt_diagnostics import receipt_date_candidates
from app.services.validation import today, validate_transaction
from app.telegram.client import normalize_image

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = ROOT / "tests/evals/extraction_cases.json"
BASELINE_REF = "34b136acecbbcf6d163fab092f3960fd780d7c91"
T = TypeVar("T", bound=BaseModel)


def digest(value: bytes | str) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def load_cases(
    path: Path, case_ids: set[str] | None = None, limit: int | None = None
) -> list[dict]:
    cases = json.loads(path.read_text())
    if not isinstance(cases, list) or not cases:
        raise ValueError("A nonempty case list is required")
    seen = set()
    for case in cases:
        case_id = case["id"]
        if not re.fullmatch(r"[a-z0-9_]{1,80}", case_id) or case_id in seen:
            raise ValueError("Case IDs must be unique, safe identifiers")
        seen.add(case_id)
        if case["kind"] not in {"text", "receipt_transcription", "receipt_image"}:
            raise ValueError("Unsupported case kind")
        if case["provenance"] not in {
            "synthetic",
            "synthetic_transcription",
            "synthetic_image",
            "real_local_image",
            "user_provided_example",
            "existing_project_example",
        }:
            raise ValueError("Explicit supported provenance is required")
        if case["kind"] == "receipt_image":
            if not isinstance(case.get("image_path"), str):
                raise ValueError("Image cases require a local image_path")
        elif not isinstance(case.get("input"), str) or not 1 <= len(case["input"]) <= 6000:
            raise ValueError("Text cases require a bounded input")
        expected = case["expected"]
        if expected["intent"] not in {"expense", "income", "unknown", "report", "advice"}:
            raise ValueError("Unsupported expected intent")
        positive = expected["intent"] in {"expense", "income"}
        if positive and (type(expected["amount"]) is not int or expected["amount"] <= 0):
            raise ValueError("Transaction labels require a positive integer amount")
        if not positive and expected["amount"] is not None:
            raise ValueError("Nontransaction labels must not include an amount")
        merchants = expected["merchants"]
        if (
            not isinstance(merchants, list)
            or not merchants
            or any(value is not None and not isinstance(value, str) for value in merchants)
        ):
            raise ValueError("Merchant labels must be a nonempty list of strings or null")
        descriptions = expected["descriptions"]
        if descriptions is not None and (
            not isinstance(descriptions, list)
            or any(not isinstance(value, str) or not value.strip() for value in descriptions)
        ):
            raise ValueError("Transactions require labeled description alternatives")
        if expected.get("date"):
            date.fromisoformat(expected["date"])
    if case_ids and not case_ids.issubset(seen):
        raise ValueError("An unknown case ID was requested")
    selected = [case for case in cases if not case_ids or case["id"] in case_ids]
    return selected[:limit] if limit else selected


def normalized(value: str | None) -> str | None:
    return " ".join(re.findall(r"\w+", value.casefold())) if value is not None else None


def score_extraction(case: dict, result: Extraction | None) -> dict[str, bool | None]:
    """Exact labels with case/whitespace/punctuation normalization, never an LLM judge."""
    expected = case["expected"]
    positive = expected["intent"] in {"expense", "income"}
    transaction = result.transaction if result else None
    merchant = transaction.merchant if transaction else None
    scores: dict[str, bool | None] = {
        "intent_match": result is not None and result.intent == expected["intent"],
        "amount_match": bool(transaction and transaction.amount == expected["amount"])
        if positive
        else None,
        "merchant_match": result is not None
        and normalized(merchant) in {normalized(value) for value in expected["merchants"]},
        "description_match": bool(
            transaction
            and normalized(transaction.description)
            in {normalized(value) for value in expected["descriptions"]}
        )
        if positive and expected["descriptions"]
        else None,
        "date_match": bool(
            transaction and transaction.transaction_date.isoformat() == expected["date"]
        )
        if expected.get("date")
        else None,
        "quantity_match": bool(
            transaction
            and transaction.receipt
            and [item.quantity for item in transaction.receipt.items] == expected["quantities"]
        )
        if "quantities" in expected
        else None,
        "hallucinated_merchant": merchant is not None
        and normalized(merchant) not in {normalized(value) for value in expected["merchants"]},
        "unsupported_transaction": not positive and transaction is not None,
    }
    scores["semantic_success"] = (
        all(value for key, value in scores.items() if key.endswith("_match") and value is not None)
        and not scores["unsupported_transaction"]
    )
    return scores


def git_source(ref: str, path: str) -> str:
    return subprocess.run(
        ["git", "show", f"{ref}:{path}"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout


def prompt_constants(source: str) -> dict[str, str]:
    """Read constant prompt concatenations without executing the source module."""
    values: dict[str, str] = {}

    def evaluate(node: ast.AST) -> str:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return values[node.id]
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return evaluate(node.left) + evaluate(node.right)
        raise ValueError("Baseline prompts must be string constants")

    for node in ast.parse(source).body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            values[node.targets[0].id] = evaluate(node.value)
    return values


@dataclass
class Baseline:
    nodes: type
    normalize: Any
    revision: str
    hashes: dict[str, str]


def load_baseline(ref: str) -> Baseline:
    """Load only trusted historical read-only boundaries, never a historical graph/stage method."""
    revision = subprocess.run(
        ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    sources = {
        name: git_source(revision, name)
        for name in (
            "app/agents/prompts/system.py",
            "app/agents/nodes/finance.py",
            "app/telegram/client.py",
        )
    }
    prompts = prompt_constants(sources["app/agents/prompts/system.py"])
    node_class = next(
        node
        for node in ast.parse(sources["app/agents/nodes/finance.py"]).body
        if isinstance(node, ast.ClassDef) and node.name == "FinanceNodes"
    )
    node_class.body = [
        node
        for node in node_class.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in {"__init__", "ocr", "extract", "validate"}
    ]
    normalizer = next(
        node
        for node in ast.parse(sources["app/telegram/client.py"]).body
        if isinstance(node, ast.FunctionDef) and node.name == "normalize_image"
    )
    namespace = {
        **prompts,
        "json": json,
        "log": logging.getLogger("finance-eval-baseline"),
        "OCR": OCR,
        "Extraction": Extraction,
        "InvalidInput": InvalidInput,
        "InvalidModelOutput": InvalidModelOutput,
        "today": today,
        "validate_transaction": validate_transaction,
        "receipt_date_candidates": receipt_date_candidates,
        "warnings": warnings,
        "Image": Image,
        "ImageOps": ImageOps,
        "UnidentifiedImageError": UnidentifiedImageError,
        "io": io,
    }
    # These are selected functions from a trusted local Git revision, not dataset-provided code.
    module = ast.fix_missing_locations(ast.Module(body=[node_class, normalizer], type_ignores=[]))
    exec(compile(module, "<trusted-baseline-boundaries>", "exec"), namespace)
    return Baseline(
        namespace["FinanceNodes"],
        namespace["normalize_image"],
        revision,
        {
            **{name: digest(source) for name, source in sources.items()},
            "extraction_prompt": digest(prompts["EXTRACTION_PROMPT"]),
            "ocr_prompt": digest(prompts["OCR_PROMPT"]),
        },
    )


@dataclass
class Observations:
    calls: list[dict] = field(default_factory=list)
    usages: list[dict] = field(default_factory=list)
    http_attempts: int = 0
    requests: int = 0
    extraction: Extraction | None = None


class EvaluationClient(OpenRouterClient):
    """Observe response metadata in memory; never serialize prompts or model text."""

    observations: Observations

    async def count_http_attempt(self, request: httpx.Request) -> None:
        self.observations.http_attempts += 1

    async def _request(self, payload: dict, role: Role, amount: int) -> dict:
        self.observations.requests += 1
        raw = await super()._request(payload, role, amount)
        usage = raw.get("usage", {})
        self.observations.usages.append(
            {key: usage.get(key) for key in ("prompt_tokens", "completion_tokens", "cost")}
        )
        return raw

    async def structured(
        self, role: Role, system: str, data: str, schema: type[T], image: bytes | None = None
    ) -> T:
        call = {"role": role, "structured_valid": False}
        self.observations.calls.append(call)
        try:
            result = await super().structured(role, system, data, schema, image=image)
        except InvalidModelOutput:
            call["structured_valid"] = False
            raise
        except Exception:
            call["structured_valid"] = None  # Transport/budget errors are not schema failures.
            raise
        call["structured_valid"] = True
        if isinstance(result, Extraction):
            self.observations.extraction = result
        return result


def complete_usage(observations: Observations, key: str) -> float | int | None:
    """A failed or retried HTTP attempt may have been billed without returned metadata."""
    if observations.http_attempts != len(observations.usages):
        return None
    if not observations.http_attempts:
        return 0
    values = [usage.get(key) for usage in observations.usages]
    if any(
        type(value) not in {int, float} or not math.isfinite(value) or value < 0 for value in values
    ):
        return None
    return sum(values)


async def evaluate_case(
    case: dict,
    variant: str,
    run: int,
    llm: EvaluationClient,
    nodes: Any,
    normalizer: Any,
    manifest_dir: Path,
) -> dict:
    llm.observations = Observations()
    token = request_id.set(f"extraction-eval-{variant}-{case['id']}-{run}")
    started, cpu_started = time.perf_counter(), time.process_time()
    stage, error_type, business_valid = "input", None, False
    extraction = None
    state: dict = {"input_source": "text" if case["kind"] == "text" else "receipt"}
    try:
        if case["kind"] == "receipt_image":
            path = await asyncio.to_thread(Path(case["image_path"]).expanduser)
            path = path if path.is_absolute() else manifest_dir / path
            if (
                await asyncio.to_thread(path.stat)
            ).st_size > nodes.settings.max_receipt_file_size_mb * 1024 * 1024:
                raise InvalidInput("Image exceeds configured size limit")
            state["image"] = await asyncio.to_thread(
                normalizer, await asyncio.to_thread(path.read_bytes)
            )
            stage = "ocr"
            state.update(await nodes.ocr(state))
        else:
            state["text"] = case["input"]
        stage = "extraction"
        state.update(await nodes.extract(state))
        extraction = state["extraction"]
        stage = "business_validation"
        if extraction.transaction is not None:
            await nodes.validate(state)
        business_valid = True
        stage = "complete"
    except Exception as exc:
        # No exception messages, raw documents, model output or credentials in the report.
        error_type = type(exc).__name__
    finally:
        request_id.reset(token)
    observation = llm.observations
    # Preserve a structured unknown result when the receipt boundary safely rejected it.
    result = extraction or observation.extraction
    scores = score_extraction(case, result)
    positive = case["expected"]["intent"] in {"expense", "income"}
    success = bool(scores["semantic_success"] and (business_valid or not positive))
    role_counts = {
        role: sum(call["role"] == role for call in observation.calls)
        for role in ("vision", "extraction")
    }
    semantic_retries = sum(max(0, count - 1) for count in role_counts.values())
    transport_retries = max(0, observation.http_attempts - observation.requests)
    return {
        "case_id": case["id"],
        "kind": case["kind"],
        "provenance": case["provenance"],
        "expected_transaction": positive,
        "variant": variant,
        "run": run,
        **scores,
        "success": success,
        "business_valid": business_valid,
        "extraction_schema_valid": result is not None,
        "structured_calls": observation.calls,
        "semantic_retries": semantic_retries,
        "transport_retries": transport_retries,
        "http_attempts": observation.http_attempts,
        "first_pass_success": success and not semantic_retries and not transport_retries,
        "latency_ms": round((time.perf_counter() - started) * 1000, 3),
        "cpu_ms": round((time.process_time() - cpu_started) * 1000, 3),
        "input_tokens": complete_usage(observation, "prompt_tokens"),
        "output_tokens": complete_usage(observation, "completion_tokens"),
        "provider_cost_usd": complete_usage(observation, "cost"),
        "last_stage": stage,
        "error_type": error_type,
    }


def fraction(values: list[bool]) -> float | None:
    return sum(values) / len(values) if values else None


def percentile(values: list[float], quantile: float) -> float | None:
    """Linear interpolation; defined for a single observation too."""
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def summarize(rows: list[dict]) -> dict:
    metrics: dict[str, Any] = {
        "evaluated": len(rows),
        "successful": sum(row["success"] for row in rows),
    }
    for name in (
        "intent_match",
        "amount_match",
        "merchant_match",
        "description_match",
        "date_match",
        "quantity_match",
        "hallucinated_merchant",
        "unsupported_transaction",
        "success",
        "business_valid",
        "extraction_schema_valid",
        "first_pass_success",
    ):
        eligible = [row[name] for row in rows if row[name] is not None]
        metrics[name] = {"rate": fraction(eligible), "denominator": len(eligible)}
    structured = [
        call["structured_valid"]
        for row in rows
        for call in row["structured_calls"]
        if call["structured_valid"] is not None
    ]
    metrics["structured_output_validity"] = {
        "rate": fraction(structured),
        "denominator": len(structured),
    }
    for kind in ("receipt_image", "receipt_transcription"):
        eligible = [
            row["success"] for row in rows if row["kind"] == kind and row["expected_transaction"]
        ]
        metrics[kind + "_success"] = {"rate": fraction(eligible), "denominator": len(eligible)}
    metrics["retry_frequency"] = fraction(
        [bool(row["semantic_retries"] or row["transport_retries"]) for row in rows]
    )
    metrics["semantic_retry_frequency"] = fraction([bool(row["semantic_retries"]) for row in rows])
    metrics["transport_retry_frequency"] = fraction(
        [bool(row["transport_retries"]) for row in rows]
    )
    metrics["latency_ms"] = {
        "p50": percentile([row["latency_ms"] for row in rows], 0.5),
        "p95": percentile([row["latency_ms"] for row in rows], 0.95),
    }
    metrics["cpu_ms"] = {
        "total": sum(row["cpu_ms"] for row in rows),
        "p50": percentile([row["cpu_ms"] for row in rows], 0.5),
    }
    for key in ("input_tokens", "output_tokens", "provider_cost_usd"):
        metrics[key] = (
            sum(row[key] for row in rows)
            if rows and all(row[key] is not None for row in rows)
            else None
        )
    total_cost = metrics["provider_cost_usd"]
    metrics["cost_usd_per_successful_transaction"] = (
        total_cost / sum(row["success"] and row["expected_transaction"] for row in rows)
        if total_cost is not None
        and any(row["success"] and row["expected_transaction"] for row in rows)
        else None
    )
    return metrics


async def run_live(args: argparse.Namespace, cases: list[dict]) -> dict:
    settings = get_settings()
    if any(
        getattr(settings, f"openrouter_{role}_model") != "openai/gpt-5.4-nano"
        for role in ("vision", "extraction")
    ):
        raise ValueError(
            "Evaluation requires the existing GPT-5.4 Nano extraction and vision models"
        )
    baseline = load_baseline(args.baseline_ref) if args.variant in {"both", "baseline"} else None
    db = Database(settings)
    rows: list[dict] = []
    aborted = None
    try:
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as http:
            llm = EvaluationClient(settings, TokenBudget(db, settings.daily_llm_token_limit), http)
            llm.observations = Observations()
            http.event_hooks["request"].append(llm.count_http_attempt)
            current = FinanceNodes(db, settings, llm)
            original = baseline.nodes(db, settings, llm) if baseline else None
            variants = ["baseline", "current"] if args.variant == "both" else [args.variant]
            for run in range(1, args.runs + 1):
                for index, case in enumerate(cases):
                    # Alternate pair order to reduce systematic warm-up/order bias.
                    order = variants if (run + index) % 2 else list(reversed(variants))
                    for variant in order:
                        row = await evaluate_case(
                            case,
                            variant,
                            run,
                            llm,
                            original if variant == "baseline" else current,
                            baseline.normalize if variant == "baseline" else normalize_image,
                            args.cases.parent,
                        )
                        rows.append(row)
                        if row["error_type"] in {
                            BudgetExceeded.__name__,
                            ModelUnavailable.__name__,
                        }:
                            aborted = row["error_type"]
                            break
                    if aborted:
                        break
                if aborted:
                    break
    finally:
        await db.close()
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return {
        "report_version": 1,
        "date_jakarta": today().isoformat(),
        "comparison_scope": "historical extraction/OCR boundaries and normalization; shared current transport, schemas, validator",
        "financial_writes": False,
        "token_accounting_writes": True,
        "baseline_revision": baseline.revision if baseline else None,
        "baseline_hashes": baseline.hashes if baseline else None,
        "current_prompt_hashes": {
            "extraction": digest(EXTRACTION_PROMPT),
            "ocr": digest(OCR_PROMPT),
        },
        "current_source_hashes": {
            name: digest((ROOT / name).read_bytes())
            for name in (
                "app/agents/nodes/finance.py",
                "app/services/extraction.py",
                "app/telegram/client.py",
                "app/llm/client.py",
            )
        },
        "dataset_hash": digest(args.cases.read_bytes()),
        "selected_case_ids": [case["id"] for case in cases],
        "runs_per_case": args.runs,
        "model": settings.openrouter_extraction_model,
        "model_settings": {
            name: getattr(settings, name)
            for name in (
                "vision_max_output_tokens",
                "extraction_max_output_tokens",
                "openrouter_max_attempts",
                "openrouter_timeout_seconds",
            )
        },
        "process_peak_rss_bytes": peak if sys.platform == "darwin" else peak * 1024,
        "memory_scope": "whole evaluator process high-water RSS; not isolated per variant",
        "aborted": aborted,
        "planned_evaluations": len(cases) * args.runs * (2 if args.variant == "both" else 1),
        "metrics": {
            variant: summarize([row for row in rows if row["variant"] == variant])
            for variant in ("baseline", "current")
            if any(row["variant"] == variant for row in rows)
        },
        "observations": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live", action="store_true", help="Authorize billed API calls and token-accounting writes"
    )
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument(
        "--case-ids", help="Comma-separated IDs; useful for a small paired smoke evaluation"
    )
    parser.add_argument("--limit", type=int, choices=range(1, 101))
    parser.add_argument("--runs", type=int, choices=range(1, 11), default=1)
    parser.add_argument("--variant", choices=("baseline", "current", "both"), default="both")
    parser.add_argument("--baseline-ref", default=BASELINE_REF, help="Trusted local Git revision")
    parser.add_argument(
        "--output", type=Path, help="Sanitized JSON report, preferably outside the checkout"
    )
    args = parser.parse_args()
    try:
        ids = {value.strip() for value in args.case_ids.split(",")} if args.case_ids else None
        cases = load_cases(args.cases, ids, args.limit)
        report = (
            asyncio.run(run_live(args, cases))
            if args.live
            else {
                "live": False,
                "validated_cases": len(cases),
                "dataset_hash": digest(args.cases.read_bytes()),
                "kind_counts": {
                    kind: sum(case["kind"] == kind for case in cases)
                    for kind in ("text", "receipt_transcription", "receipt_image")
                },
                "model_calls": 0,
                "financial_writes": False,
                "token_accounting_writes": False,
            }
        )
        serialized = json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
        if args.output:
            args.output.write_text(serialized)
        else:
            print(serialized, end="")
        if report.get("aborted"):
            raise SystemExit(2)
    except Exception as exc:
        print(json.dumps({"evaluation_error": type(exc).__name__}), file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()

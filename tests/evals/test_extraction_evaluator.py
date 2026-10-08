"""Deterministic evaluator tests: no model calls or PostgreSQL connection."""

import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from app.core.errors import InvalidModelOutput
from app.schemas.finance import Extraction
from scripts.evaluate_extraction import (
    DEFAULT_CASES,
    Observations,
    complete_usage,
    evaluate_case,
    load_baseline,
    load_cases,
    percentile,
    prompt_constants,
    score_extraction,
    summarize,
)


def example_result(draft, **overrides):
    return Extraction.model_validate(
        {
            "intent": "expense",
            "transaction": {**draft, **overrides},
            "period": None,
        }
    )


def example_case():
    return {
        "id": "spbu",
        "kind": "text",
        "provenance": "synthetic",
        "input": "Aku menambal ban di SPBU seharga 15.000",
        "expected": {
            "intent": "expense",
            "amount": 15000,
            "merchants": ["SPBU"],
            "descriptions": ["Menambal ban", "Tire repair"],
        },
    }


def test_dataset_has_labeled_negative_quantity_and_receipt_cases():
    cases = load_cases(DEFAULT_CASES)
    assert len(cases) >= 25
    assert all(case["kind"] != "receipt_image" for case in cases)
    assert any(case["expected"].get("quantities") == ["1.5"] for case in cases)
    assert len([case for case in cases if case["expected"]["merchants"] == [None]]) >= 8
    assert {case["id"] for case in load_cases(DEFAULT_CASES, {"generic_spbu_id"})} == {
        "generic_spbu_id"
    }
    assert len(load_cases(DEFAULT_CASES, limit=2)) == 2
    with pytest.raises(ValueError):
        load_cases(DEFAULT_CASES, {"missing"})


def test_dataset_rejects_duplicate_and_unsafe_ids(tmp_path):
    path = tmp_path / "cases.json"
    path.write_text(json.dumps([example_case(), example_case()]))
    with pytest.raises(ValueError):
        load_cases(path)
    case = example_case()
    case["id"] = "private/raw-name"
    path.write_text(json.dumps([case]))
    with pytest.raises(ValueError):
        load_cases(path)


def test_normalized_alternatives_and_merchant_hallucination(draft):
    case = example_case()
    correct = example_result(draft, amount=15000, merchant="spbu", description="Tire repair.")
    assert score_extraction(case, correct)["semantic_success"] is True
    hallucinated = correct.model_copy(
        update={"transaction": correct.transaction.model_copy(update={"merchant": "Pertamina"})}
    )
    assert score_extraction(case, hallucinated)["hallucinated_merchant"] is True
    assert score_extraction(case, hallucinated)["semantic_success"] is False
    missing = correct.model_copy(
        update={"transaction": correct.transaction.model_copy(update={"merchant": None})}
    )
    assert score_extraction(case, missing)["merchant_match"] is False
    assert score_extraction(case, missing)["hallucinated_merchant"] is False


def test_no_judge_and_unlabeled_description_is_not_counted(draft):
    case = example_case()
    case["expected"]["descriptions"] = None
    result = example_result(draft, amount=15000, merchant="SPBU", description="Unscored wording")
    assert score_extraction(case, result)["description_match"] is None
    assert score_extraction(case, result)["semantic_success"] is True


def test_missing_response_is_failure_and_unknown_has_no_amount_denominator(draft):
    assert score_extraction(example_case(), None)["semantic_success"] is False
    case = example_case()
    case["expected"] = {
        "intent": "unknown",
        "amount": None,
        "merchants": [None],
        "descriptions": [],
    }
    unknown = Extraction(intent="unknown", transaction=None, period=None)
    scores = score_extraction(case, unknown)
    assert scores["amount_match"] is None
    assert scores["description_match"] is None
    assert scores["semantic_success"] is True
    assert score_extraction(case, example_result(draft))["unsupported_transaction"] is True


def test_usage_never_fabricates_missing_or_uncertain_provider_data():
    assert complete_usage(Observations(), "cost") == 0
    observations = Observations(
        http_attempts=1,
        usages=[
            {
                "prompt_tokens": 12,
                "completion_tokens": 8,
                "cost": None,
            }
        ],
    )
    assert complete_usage(observations, "prompt_tokens") == 12
    assert complete_usage(observations, "cost") is None
    observations.http_attempts = 2
    assert complete_usage(observations, "prompt_tokens") is None
    observations.http_attempts = 1
    observations.usages[0]["cost"] = float("nan")
    assert complete_usage(observations, "cost") is None


def test_baseline_prompts_are_parsed_without_execution():
    assert prompt_constants('BOUNDARY = "a"\nEXTRACTION_PROMPT = BOUNDARY + "b"') == {
        "BOUNDARY": "a",
        "EXTRACTION_PROMPT": "ab",
    }
    with pytest.raises(ValueError):
        prompt_constants('BOUNDARY = open("secret").read()')


def test_historical_boundary_excludes_write_methods(monkeypatch):
    import scripts.evaluate_extraction as module

    sources = {
        "app/agents/prompts/system.py": 'BOUNDARY="x"\nOCR_PROMPT="ocr"\nEXTRACTION_PROMPT="extract"',
        "app/agents/nodes/finance.py": (
            "class FinanceNodes:\n"
            " def __init__(self, db, settings, llm): self.db = db\n"
            " async def extract(self, state): return {}\n"
            ' async def stage(self, state): raise AssertionError("write")\n'
            ' async def callback(self, state): raise AssertionError("write")\n'
        ),
        "app/telegram/client.py": "def normalize_image(raw): return raw\n",
    }
    monkeypatch.setattr(module, "git_source", lambda ref, path: sources[path])
    monkeypatch.setattr(
        module.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout="a" * 40)
    )
    baseline = load_baseline("trusted-ref")
    assert hasattr(baseline.nodes, "extract")
    assert not hasattr(baseline.nodes, "stage")
    assert not hasattr(baseline.nodes, "callback")
    assert baseline.normalize(b"input") == b"input"


async def test_evaluation_only_calls_extraction_validation_and_redacts(draft, tmp_path):
    result = example_result(draft, amount=15000, merchant="SPBU", description="Menambal ban")
    llm = SimpleNamespace(observations=Observations())
    nodes = SimpleNamespace(
        extract=AsyncMock(return_value={"extraction": result}),
        validate=AsyncMock(return_value={}),
        stage=AsyncMock(),
        settings=SimpleNamespace(max_receipt_file_size_mb=5),
    )
    row = await evaluate_case(example_case(), "current", 1, llm, nodes, None, tmp_path)
    assert row["success"] is True
    nodes.extract.assert_awaited_once()
    nodes.validate.assert_awaited_once()
    nodes.stage.assert_not_awaited()
    report = json.dumps(row)
    assert "Menambal ban" not in report
    assert "SPBU" not in report
    assert "Point Coffee" not in report


async def test_validation_failure_cannot_count_as_success(draft, tmp_path):
    result = example_result(draft, amount=15000, merchant="SPBU", description="Menambal ban")
    llm = SimpleNamespace(observations=Observations())
    nodes = SimpleNamespace(
        extract=AsyncMock(return_value={"extraction": result}),
        validate=AsyncMock(side_effect=InvalidModelOutput()),
        stage=AsyncMock(),
    )
    row = await evaluate_case(example_case(), "current", 1, llm, nodes, None, tmp_path)
    assert row["semantic_success"] is True
    assert row["business_valid"] is False
    assert row["success"] is False
    nodes.stage.assert_not_awaited()


async def test_receipt_image_runs_ocr_before_extraction(draft, tmp_path):
    image = io.BytesIO()
    Image.new("RGB", (10, 10), "white").save(image, format="JPEG")
    (tmp_path / "private.jpg").write_bytes(image.getvalue())
    result = example_result(draft, amount=15000, merchant="SPBU", description="Menambal ban")
    case = {
        **example_case(),
        "kind": "receipt_image",
        "provenance": "synthetic_image",
        "image_path": "private.jpg",
    }
    llm = SimpleNamespace(observations=Observations())
    nodes = SimpleNamespace(
        ocr=AsyncMock(return_value={"text": "private receipt"}),
        extract=AsyncMock(return_value={"extraction": result}),
        validate=AsyncMock(return_value={}),
        stage=AsyncMock(),
        settings=SimpleNamespace(max_receipt_file_size_mb=5),
    )
    row = await evaluate_case(case, "current", 1, llm, nodes, lambda raw: raw, tmp_path)
    assert nodes.extract.call_args.args[0]["text"] == "private receipt"
    assert row["kind"] == "receipt_image"
    assert "private" not in json.dumps(row)
    nodes.stage.assert_not_awaited()


def test_percentiles_and_empty_metrics_are_explicitly_unavailable():
    assert percentile([], 0.5) is None
    assert percentile([10], 0.95) == 10
    assert percentile([0, 100], 0.95) == 95
    metrics = summarize([])
    assert metrics["receipt_image_success"] == {"rate": None, "denominator": 0}
    assert metrics["provider_cost_usd"] is None
    assert metrics["cost_usd_per_successful_transaction"] is None


async def test_summary_excludes_unknown_amounts_and_missing_cost(draft, tmp_path):
    result = example_result(draft, amount=15000, merchant="SPBU", description="Menambal ban")
    llm = SimpleNamespace(observations=Observations())
    nodes = SimpleNamespace(
        extract=AsyncMock(return_value={"extraction": result}), validate=AsyncMock(return_value={})
    )
    row = await evaluate_case(example_case(), "current", 1, llm, nodes, None, tmp_path)
    row["provider_cost_usd"] = None
    unknown = {
        **row,
        "expected_transaction": False,
        "amount_match": None,
        "description_match": None,
    }
    metrics = summarize([row, unknown])
    assert metrics["amount_match"] == {"rate": 1, "denominator": 1}
    assert metrics["provider_cost_usd"] is None
    assert metrics["receipt_image_success"]["rate"] is None

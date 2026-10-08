"""Deterministic workflow tests; mocked outputs do not measure model accuracy."""

import json
from unittest.mock import AsyncMock

import pytest

from app.agents.graphs.workflows import receipt_graph, transaction_graph
from app.agents.nodes.finance import FinanceNodes
from app.core.errors import InvalidModelOutput, ModelUnavailable
from app.schemas.finance import OCR, Extraction


def extracted(draft, *, receipt=False, merchant=None):
    return Extraction.model_validate(
        {
            "intent": "expense",
            "period": None,
            "transaction": {
                **draft,
                "merchant": merchant,
                "receipt": {
                    "items": [],
                    "subtotal": 25000,
                    "discount": 0,
                    "tax": 0,
                    "service_charge": 0,
                    "grand_total": 25000,
                }
                if receipt
                else None,
            },
        }
    )


def nodes_for(settings, responses):
    llm = AsyncMock()
    llm.structured.side_effect = responses
    nodes = FinanceNodes(None, settings, llm)
    nodes.stage = AsyncMock(return_value={"pending_id": "mock-pending"})
    nodes.preview = AsyncMock(return_value={"response": {"text": "preview"}})
    return nodes, llm


async def test_text_one_recovery_uses_original_evidence_and_targeted_prompt(settings, draft):
    nodes, llm = nodes_for(settings, [InvalidModelOutput(), extracted(draft)])
    result = await transaction_graph(nodes).ainvoke(
        {"text": "Beli kopi 25 ribu", "input_source": "text"}
    )
    assert result["response"]["text"] == "preview"
    assert result["recovery_attempts"] == 1
    calls = llm.structured.call_args_list
    assert len(calls) == 2
    assert calls[0].args[2] == calls[1].args[2]
    assert calls[0].args[1] != calls[1].args[1]
    nodes.stage.assert_awaited_once()


@pytest.mark.parametrize(
    "error",
    [InvalidModelOutput(), ModelUnavailable(), InvalidModelOutput("refusal", retryable=False)],
)
async def test_recovery_failure_never_stages(settings, error):
    responses = [error, error] if getattr(error, "retryable", False) else [error]
    nodes, llm = nodes_for(settings, responses)
    result = await transaction_graph(nodes).ainvoke({"text": "Beli kopi 25 ribu"})
    assert result["error"]
    assert llm.structured.await_count == len(responses)
    nodes.stage.assert_not_awaited()


async def test_receipt_shares_single_recovery_allowance_across_nodes(settings):
    nodes, llm = nodes_for(
        settings,
        [
            InvalidModelOutput(),
            OCR(transcription="Total 25.000", readable=True),
            InvalidModelOutput(),
        ],
    )
    result = await receipt_graph(nodes).ainvoke({"image": b"mock", "input_source": "receipt"})
    assert result["error"]
    assert llm.structured.await_count == 3
    assert [call.args[0] for call in llm.structured.call_args_list] == [
        "vision",
        "vision",
        "extraction",
    ]
    nodes.stage.assert_not_awaited()


async def test_receipt_extraction_recovery_stages_once(settings, draft):
    nodes, llm = nodes_for(
        settings,
        [
            OCR(transcription="Coffee\nTotal 25.000", readable=True),
            InvalidModelOutput(),
            extracted(draft, receipt=True),
        ],
    )
    result = await receipt_graph(nodes).ainvoke({"image": b"mock", "input_source": "receipt"})
    assert result["response"]["text"] == "preview"
    assert llm.structured.await_count == 3
    nodes.stage.assert_awaited_once()


@pytest.mark.parametrize("receipt", [False, True])
async def test_unknown_optional_merchant_never_retries(settings, draft, receipt):
    responses = [extracted(draft, receipt=receipt)]
    if receipt:
        responses.insert(0, OCR(transcription="Coffee\nTotal 25.000", readable=True))
    nodes, llm = nodes_for(settings, responses)
    graph = receipt_graph(nodes) if receipt else transaction_graph(nodes)
    await graph.ainvoke(
        {
            "text": "Beli kopi 25 ribu",
            "image": b"mock",
            "input_source": "receipt" if receipt else "text",
        }
    )
    assert llm.structured.await_count == (2 if receipt else 1)
    nodes.stage.assert_awaited_once()


async def test_unknown_intent_and_unreadable_receipt_request_new_evidence(settings):
    nodes, llm = nodes_for(settings, [Extraction(intent="unknown", transaction=None, period=None)])
    result = await transaction_graph(nodes).ainvoke({"text": "Mungkin beli bensin"})
    assert "No record" in result["response"]["text"]
    assert llm.structured.await_count == 1
    nodes.stage.assert_not_awaited()
    nodes, llm = nodes_for(settings, [OCR(transcription="?", readable=False)])
    result = await receipt_graph(nodes).ainvoke({"image": b"mock", "input_source": "receipt"})
    assert "document" in result["response"]["text"]
    assert llm.structured.await_count == 1
    nodes.stage.assert_not_awaited()


async def test_long_transcription_envelope_is_passed_intact(settings, draft):
    source = "Coffee 25.000\n" + "x" * 5986
    nodes, llm = nodes_for(settings, [extracted(draft, receipt=True)])
    await nodes.extract({"text": source, "input_source": "receipt"})
    assert json.loads(llm.structured.call_args.args[2])["untrusted_text"] == source


@pytest.mark.parametrize(
    "failure", ["missing_transaction", "nested_intent", "unexpected_transaction"]
)
async def test_schema_valid_contract_failure_recovers_once(settings, draft, failure):
    valid = extracted(draft)
    bad = (
        Extraction(intent="expense", transaction=None, period=None)
        if failure == "missing_transaction"
        else valid.model_copy(
            update={"intent": "income" if failure == "nested_intent" else "unknown"}
        )
    )
    nodes, llm = nodes_for(settings, [bad, valid])
    result = await transaction_graph(nodes).ainvoke(
        {"text": "Transfer 25000 untuk kopi", "input_source": "text"}
    )
    assert result["response"]["text"] == "preview"
    assert llm.structured.await_count == 2
    nodes.stage.assert_awaited_once()


async def test_repeated_contract_failure_stops_without_staging(settings):
    bad = Extraction(intent="expense", transaction=None, period=None)
    nodes, llm = nodes_for(settings, [bad, bad])
    result = await transaction_graph(nodes).ainvoke({"text": "Beli kopi 25000"})
    assert result["error"]
    assert llm.structured.await_count == 2
    nodes.stage.assert_not_awaited()


async def test_contract_failure_cannot_exceed_used_ocr_allowance(settings):
    nodes, llm = nodes_for(
        settings,
        [
            InvalidModelOutput(),
            OCR(transcription="Total 25000", readable=True),
            Extraction(intent="expense", transaction=None, period=None),
        ],
    )
    result = await receipt_graph(nodes).ainvoke({"image": b"mock", "input_source": "receipt"})
    assert result["error"]
    assert llm.structured.await_count == 3
    nodes.stage.assert_not_awaited()

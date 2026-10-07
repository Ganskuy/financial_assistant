import json
import logging
from datetime import date
from unittest.mock import AsyncMock

import pytest

from app.agents.graphs.workflows import receipt_graph
from app.agents.nodes.finance import FinanceNodes
from app.core.errors import InvalidModelOutput
from app.core.logging import SafeJSONFormatter
from app.schemas.finance import OCR, Extraction
from app.services.receipt_diagnostics import receipt_date_candidates


def test_date_candidates_exclude_identifiers():
    text = "06/10/2026 06 Okt 2026 2026-10-06 Phone 081234567890 RCT 9126100600190870"
    assert receipt_date_candidates(text) == ["06/10/2026", "06 Okt 2026", "2026-10-06"]
    assert receipt_date_candidates("receipt without a date") == []
    assert len(receipt_date_candidates(" ".join(f"{day}/10/2026" for day in range(1, 20)))) == 8


def test_formatter_preserves_dates_but_excludes_raw_receipt():
    record = logging.makeLogRecord(
        {
            "msg": "receipt_ocr_dates",
            "levelname": "INFO",
            "ocr_date_candidates": ["06/10/2026"],
            "transaction_date": "2026-10-06",
            "current_date": "2026-10-07",
            "transcription": "private receipt text",
        }
    )
    result = json.loads(SafeJSONFormatter().format(record))
    assert result["ocr_date_candidates"] == ["06/10/2026"]
    assert result["current_date"] == "2026-10-07"
    assert "private receipt text" not in json.dumps(result)


@pytest.mark.parametrize("printed", ["06/10/2026", "06 Okt 2026"])
@pytest.mark.parametrize(
    "extracted,accepted",
    [
        ("2026-10-06", True),
        ("2026-10-07", True),
        ("2026-10-08", False),
        ("2027-10-06", False),
        ("1999-10-06", False),
        ("0610-10-07", False),
    ],
)
async def test_receipt_graph_date_boundary(
    settings, draft, monkeypatch, printed, extracted, accepted
):
    monkeypatch.setattr("app.services.validation.today", lambda: date(2026, 10, 7))
    monkeypatch.setattr("app.agents.nodes.finance.today", lambda: date(2026, 10, 7))
    transcription = f"Receipt date {printed}\nTotal Rp25.000"
    payload = {
        **draft,
        "transaction_date": extracted,
        "receipt": {
            "items": [],
            "subtotal": 25000,
            "discount": 0,
            "tax": 0,
            "service_charge": 0,
            "grand_total": 25000,
        },
    }
    llm = AsyncMock()
    llm.structured.side_effect = [
        OCR.model_validate_json(json.dumps({"readable": True, "transcription": transcription})),
        Extraction.model_validate_json(
            json.dumps({"intent": "expense", "transaction": payload, "period": None})
        ),
    ]
    nodes = FinanceNodes(None, settings, llm)
    nodes.stage = AsyncMock(return_value={})
    nodes.preview = AsyncMock(return_value={"response": {"text": "preview"}})
    result = await receipt_graph(nodes).ainvoke({"image": b"test", "input_source": "receipt"})
    assert json.loads(llm.structured.call_args_list[1].args[2])["untrusted_text"] == transcription
    if accepted:
        nodes.stage.assert_awaited_once()
        assert result["response"]["text"] == "preview"
    else:
        nodes.stage.assert_not_awaited()
        assert extracted in result["response"]["text"]
        assert "2026-10-07" in result["response"]["text"]


@pytest.mark.parametrize("failure", ["unreadable", "invalid_json", "missing_transaction"])
async def test_receipt_failures_never_stage(settings, failure):
    llm = AsyncMock()
    readable = OCR(readable=True, transcription="Receipt 06/10/2026")
    if failure == "unreadable":
        llm.structured.return_value = OCR(readable=False, transcription="Unreadable")
    elif failure == "invalid_json":
        llm.structured.side_effect = [readable, InvalidModelOutput()]
    else:
        llm.structured.side_effect = [
            readable,
            Extraction(intent="unknown", transaction=None, period=None),
        ]
    nodes = FinanceNodes(None, settings, llm)
    nodes.stage = AsyncMock(return_value={})
    result = await receipt_graph(nodes).ainvoke({"image": b"test", "input_source": "receipt"})
    assert result["error"]
    nodes.stage.assert_not_awaited()

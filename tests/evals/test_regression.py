import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.schemas.finance import Advice, TransactionDraft
from app.services.advice import make_facts, render_advice
from app.services.validation import today, validate_transaction
from app.telegram.router import route_text

CASES = json.loads(Path(__file__).with_name("cases.json").read_text())


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["kind"] + ":" + c["input"][:20])
def test_fixture_policy_contract(case):
    """Offline schema/policy regression; this does not measure live model extraction accuracy."""
    if case.get("amount"):
        draft = TransactionDraft(
            intent=case["intent"],
            transaction_date=today(),
            merchant=None,
            description=case["input"][:240],
            amount=case["amount"],
            currency="IDR",
            category=case["category"],
            confidence=0.9,
            receipt=None,
        )
        assert not validate_transaction(draft)
    elif case["kind"] == "advisor_grounding":
        assert route_text(case["input"]).name == "advice"
        summary = {"period": "2026-10", "by_category": {}, **case["authoritative"]}
        rendered = render_advice(
            Advice(points=[{"action": "protect_surplus", "fact_ids": ["balance"]}]),
            make_facts(summary, [], []),
            summary,
        )
        assert "Rp1,850,000" in rendered
    else:
        assert route_text(case["input"]).name == "extract"
        with pytest.raises(ValidationError):
            TransactionDraft.model_validate({"description": case["input"]})

import json

import pytest

from app.core.errors import InvalidModelOutput
from app.schemas.finance import Advice
from app.services.advice import compact_advisor_payload, make_facts, render_advice


def test_compact_facts_preserve_values_ids_and_backend_rendering():
    summary = {
        "income": 1000000,
        "expenses": 25000,
        "balance": 975000,
        "period": "2026-10",
        "by_category": {"food": 25000},
    }
    facts = make_facts(
        summary,
        [{"category": "food", "limit": 100000, "spent": 25000, "remaining": 75000}],
        [{"target": 500000, "saved": 100000, "remaining": 400000}],
    )
    raw = compact_advisor_payload(facts)
    values = json.loads(raw.split("\n", 1)[1])
    assert values == {key: value["value"] for key, value in facts.items()}
    assert len(raw) < len("TRUSTED TOOL DATA\n" + json.dumps(facts)) / 2
    assert "label" not in raw
    advice = Advice(
        points=[
            {"action": "protect_surplus", "fact_ids": ["balance"]},
            {"action": "review_budget", "fact_ids": ["budget.food.remaining"]},
        ]
    )
    text = render_advice(advice, facts, summary)
    assert "Rp975,000" in text and "food budget remaining: Rp75,000" in text


def test_large_integer_precision_and_oversize_rejection():
    raw = compact_advisor_payload({"income": {"label": "income", "value": 2**60 + 1}})
    assert json.loads(raw.split("\n", 1)[1])["income"] == 2**60 + 1
    with pytest.raises(InvalidModelOutput):
        compact_advisor_payload({"x" * 6000: {"value": 1}})

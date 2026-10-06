import json

from app.core.errors import InvalidModelOutput
from app.schemas.finance import Advice
from app.services.finance import format_summary
from app.services.validation import idr

ACTION_TEXT = {
    "review_category": "Review purchases in this category before planning the next month.",
    "reduce_optional_spending": "Consider reducing optional purchases in this category.",
    "protect_surplus": "Consider earmarking part of the recorded surplus toward your savings goals.",
    "close_deficit": "Recorded spending exceeds income. Review optional expenses and upcoming bills.",
    "review_budget": "Compare this category's spending with its confirmed budget before another purchase.",
    "build_emergency_fund": "Consider building an emergency fund based on essential expenses and income stability.",
    "track_more_data": "Record more income and expenses before drawing a financial conclusion.",
}


def make_facts(summary: dict, budgets: list[dict], savings: list[dict]) -> dict:
    facts = {
        key: {"label": key, "value": summary[key]} for key in ("income", "expenses", "balance")
    }
    for category, value in summary["by_category"].items():
        facts[f"category.{category}"] = {"label": f"{category} spending", "value": value}
    for row in budgets:
        for key in ("limit", "spent", "remaining"):
            facts[f"budget.{row['category']}.{key}"] = {
                "label": f"{row['category']} budget {key}",
                "value": row[key],
            }
    for index, row in enumerate(savings):
        for key in ("target", "saved", "remaining"):
            facts[f"saving.{index}.{key}"] = {
                "label": f"Savings goal {index + 1} {key}",
                "value": row[key],
            }
    return facts


def compact_advisor_payload(facts: dict) -> str:
    """Labels stay on the backend; self-describing fact IDs carry exact integer values."""
    payload = "TRUSTED TOOL DATA\n" + json.dumps(
        {key: fact["value"] for key, fact in facts.items()},
        ensure_ascii=True,
        separators=(",", ":"),
    )
    if len(payload) > 6000:
        raise InvalidModelOutput()
    return payload


def render_advice(advice: Advice, facts: dict, summary: dict) -> str:
    lines = [format_summary(summary), "", "Guidance based on recorded data:"]
    for point in advice.points:
        if any(key not in facts for key in point.fact_ids):
            raise InvalidModelOutput()
        if point.action == "protect_surplus" and (
            summary["balance"] <= 0 or "balance" not in point.fact_ids
        ):
            raise InvalidModelOutput()
        if point.action == "close_deficit" and (
            summary["balance"] >= 0 or "balance" not in point.fact_ids
        ):
            raise InvalidModelOutput()
        if point.action in {"review_category", "reduce_optional_spending"} and not all(
            key.startswith("category.") for key in point.fact_ids
        ):
            raise InvalidModelOutput()
        if point.action == "review_budget" and not all(
            key.startswith("budget.") for key in point.fact_ids
        ):
            raise InvalidModelOutput()
        lines.append(ACTION_TEXT[point.action])
        lines.extend(
            f"  {facts[key]['label']}: {idr(facts[key]['value'])}" for key in point.fact_ids
        )
    return "\n".join(lines)

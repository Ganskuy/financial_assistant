"""Deterministic report facts and schema-only presentation decisions."""

import calendar
import json
import re
from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.core.errors import InvalidInput
from app.repositories.visualization import VisualizationRepository
from app.schemas.visualization import ChartSpec, VisualizationSpec
from app.services.validation import idr, month_bounds, today
from app.telegram.router import MONTHS

# An approved Sol transport can supply this callable after model/profile review.
# Production intentionally has no substitute model or unreviewed provider slug.
Analyzer = Callable[[str, type[VisualizationSpec]], Awaitable[Any]]


def parse_month(args: str) -> str:
    parts = args.strip().lower().split()
    if len(parts) == 1 and re.fullmatch(r"\d{4}-\d{2}", parts[0]):
        period = parts[0]
    elif (
        1 <= len(parts) <= 2
        and parts[0] in MONTHS
        and (len(parts) == 1 or re.fullmatch(r"\d{4}", parts[1]))
    ):
        year = int(parts[1]) if len(parts) == 2 else today().year
        period = f"{year:04d}-{MONTHS[parts[0]]:02d}"
    else:
        raise InvalidInput("Use /visualize October or /visualize October 2026 (also YYYY-MM).")
    month_bounds(period)
    return period


def percentage(value: int, base: int) -> str | None:
    if base == 0:
        return None
    return str(
        (Decimal(value) * 100 / Decimal(base)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    )


def aggregate_groups(period: str, rows: list[tuple]) -> dict:
    start, end = month_bounds(period)
    previous = (start - timedelta(days=1)).replace(day=1)
    totals = {"income": 0, "expense": 0}
    prior = {"income": 0, "expense": 0}
    counts = {"income": 0, "expense": 0}
    categories = {"income": defaultdict(lambda: [0, 0]), "expense": defaultdict(lambda: [0, 0])}
    daily = {start + timedelta(days=i): 0 for i in range((end - start).days)}
    previous_count = 0
    for day, kind, category, amount, count in rows:
        amount, count = int(amount), int(count)
        if previous <= day < start:
            prior[kind] += amount
            previous_count += count
        elif start <= day < end:
            totals[kind] += amount
            counts[kind] += count
            categories[kind][category][0] += amount
            categories[kind][category][1] += count
            if kind == "expense":
                daily[day] += amount
    weekly: dict[int, int] = defaultdict(int)
    for day, amount in daily.items():
        weekly[(day.day - 1) // 7 + 1] += amount
    net = totals["income"] - totals["expense"]
    category_data = {
        f"{kind}_by_category": [
            {
                "category": category,
                "amount": value[0],
                "percentage": percentage(value[0], totals[kind]),
                "transaction_count": value[1],
            }
            for category, value in sorted(
                categories[kind].items(), key=lambda item: (-item[1][0], item[0])
            )
        ]
        for kind in totals
    }
    return {
        "period": {
            "month": calendar.month_name[start.month],
            "year": start.year,
            "start_date": str(start),
            "end_date": str(end - timedelta(days=1)),
        },
        "overview": {
            "total_income": totals["income"],
            "total_expense": totals["expense"],
            "net_cashflow": net,
            "savings_rate_pct": percentage(net, totals["income"]),
            "income_transaction_count": counts["income"],
            "expense_transaction_count": counts["expense"],
        },
        **category_data,
        "daily_expense": [{"date": str(day), "amount": amount} for day, amount in daily.items()],
        "weekly_expense": [{"week": week, "amount": amount} for week, amount in weekly.items()],
        "comparison": {
            "previous_period_available": previous_count > 0,
            "previous_month_total_income": prior["income"] if previous_count else None,
            "previous_month_total_expense": prior["expense"] if previous_count else None,
            "income_change_pct": percentage(totals["income"] - prior["income"], prior["income"])
            if previous_count
            else None,
            "expense_change_pct": percentage(totals["expense"] - prior["expense"], prior["expense"])
            if previous_count
            else None,
        },
    }


async def aggregate(db, user_id, period: str) -> dict:
    start, end = month_bounds(period)
    previous = (start - timedelta(days=1)).replace(day=1)
    async with db.transaction() as session:
        rows = await VisualizationRepository(session, user_id).grouped(previous, end)
    return aggregate_groups(period, rows)


def compact_payload(data: dict) -> str:
    # The weekly series is derivable and adds no detail to the daily series for analysis.
    compact = {key: value for key, value in data.items() if key != "weekly_expense"}
    payload = json.dumps(compact, ensure_ascii=True, separators=(",", ":"))
    if len(payload) > 6000:
        raise ValueError("Visualization payload exceeds the existing model envelope")
    return payload


def default_spec(data: dict) -> VisualizationSpec:
    charts = [{"type": "bar", "metric": "income_vs_expense"}]
    if data["expense_by_category"]:
        charts += [
            {"type": "pie", "metric": "expense_by_category"},
            {"type": "line", "metric": "daily_expense"},
        ]
    elif data["income_by_category"]:
        charts += [{"type": "bar", "metric": "income_by_category"}]
    return VisualizationSpec.model_validate(
        {
            "report": {
                "summary": "cashflow",
                "insights": ["top_expense", "top_income", "expense_comparison"],
            },
            "charts": charts,
        }
    )


def validate_spec(raw: Any, data: dict) -> VisualizationSpec:
    if isinstance(raw, VisualizationSpec):
        raw = raw.model_dump()
    result = (
        VisualizationSpec.model_validate_json(raw)
        if isinstance(raw, str)
        else VisualizationSpec.model_validate(raw)
    )
    for chart in result.charts:
        if not resolve_metric(chart, data):
            raise ValueError("Selected metric has no observations")
    return result


def resolve_metric(spec: ChartSpec, data: dict) -> list[tuple[str, int]]:
    spec = ChartSpec.model_validate(spec.model_dump())
    metric = spec.metric
    overview = data["overview"]
    if metric in {"income_by_category", "expense_by_category"}:
        return [(row["category"], row["amount"]) for row in data[metric]]
    if metric == "daily_expense":
        return [(row["date"][-2:], row["amount"]) for row in data[metric]]
    if metric == "weekly_expense":
        return [(f"Week {row['week']}", row["amount"]) for row in data[metric]]
    if metric == "income_vs_expense":
        return [("Income", overview["total_income"]), ("Expenses", overview["total_expense"])]
    if metric == "net_cashflow":
        return [("Net cashflow", overview["net_cashflow"])]
    comparison = data["comparison"]
    if not comparison["previous_period_available"]:
        return []
    return [
        ("Previous income", comparison["previous_month_total_income"]),
        ("Current income", overview["total_income"]),
        ("Previous expenses", comparison["previous_month_total_expense"]),
        ("Current expenses", overview["total_expense"]),
    ]


def report_lines(data: dict, spec: VisualizationSpec) -> list[str]:
    p, overview = data["period"], data["overview"]
    lines = [
        f"Financial Report - {p['month']} {p['year']}",
        f"Income: {idr(overview['total_income'])}",
        f"Expenses: {idr(overview['total_expense'])}",
        f"Net cashflow: {idr(overview['net_cashflow'])}",
    ]
    for insight in dict.fromkeys(spec.report.insights):
        if insight in {"top_expense", "top_income"}:
            kind = insight.removeprefix("top_")
            values = data[f"{kind}_by_category"]
            if values:
                top = values[0]
                lines.append(f"Largest {kind} category: {top['category']} ({idr(top['amount'])}).")
        elif insight == "expense_comparison":
            comp = data["comparison"]
            if not comp["previous_period_available"]:
                lines.append("No previous-month transactions for comparison.")
            elif comp["expense_change_pct"] is None:
                lines.append("Expense change percentage unavailable: previous expenses were zero.")
            else:
                lines.append(f"Expenses vs previous month: {comp['expense_change_pct']}%.")
        elif insight == "savings_rate":
            rate = overview["savings_rate_pct"]
            lines.append(
                f"Net cashflow / income: {rate}%."
                if rate is not None
                else "Savings rate unavailable: no income recorded."
            )
    lines.append("Confirmed records only; net cashflow excludes opening money.")
    return lines

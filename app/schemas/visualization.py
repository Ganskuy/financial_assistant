"""Closed presentation decisions: the model cannot supply values or executable text."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Metric = Literal[
    "expense_by_category",
    "income_by_category",
    "daily_expense",
    "weekly_expense",
    "income_vs_expense",
    "net_cashflow",
    "monthly_comparison",
]
Insight = Literal["top_expense", "top_income", "expense_comparison", "savings_rate"]


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class ReportDecision(ClosedModel):
    summary: Literal["cashflow"]
    insights: list[Insight] = Field(min_length=1, max_length=3)


class ChartSpec(ClosedModel):
    type: Literal["bar", "line", "pie"]
    metric: Metric

    @model_validator(mode="after")
    def compatible(self) -> "ChartSpec":
        if self.type == "pie" and self.metric not in {
            "expense_by_category",
            "income_by_category",
            "income_vs_expense",
        }:
            raise ValueError("Pie charts require nonnegative distributions")
        if self.type == "line" and self.metric not in {"daily_expense", "weekly_expense"}:
            raise ValueError("Line charts require time series")
        return self


class VisualizationSpec(ClosedModel):
    report: ReportDecision
    charts: list[ChartSpec] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def unique_metrics(self) -> "VisualizationSpec":
        if len({chart.metric for chart in self.charts}) != len(self.charts):
            raise ValueError("Duplicate chart metrics")
        return self

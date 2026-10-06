import asyncio
import base64
import logging
import time

from sqlalchemy.exc import SQLAlchemyError

from app.agents.nodes.finance import FinanceNodes
from app.agents.state import WorkflowState
from app.schemas.visualization import VisualizationSpec
from app.services.visualization import (
    Analyzer,
    aggregate,
    compact_payload,
    default_spec,
    parse_month,
    report_lines,
    validate_spec,
)
from app.services.visualization_render import render_report

log = logging.getLogger("finance")


class VisualizationNodes(FinanceNodes):
    def __init__(self, db, settings, llm, analyzer: Analyzer | None = None):
        super().__init__(db, settings, llm)
        self.analyzer = analyzer

    async def parse_visualize(self, state: WorkflowState) -> dict:
        return {"period": parse_month(state["text"])}

    async def aggregate_visualize(self, state: WorkflowState) -> dict:
        started = time.monotonic()
        # Database failures deliberately propagate to the durable worker for retry.
        try:
            data = await aggregate(self.db, state["user_id"], state["period"])
        except SQLAlchemyError as exc:
            log.warning(
                "visualize_aggregation_failure",
                extra={"workflow": "visualize", "error_type": type(exc).__name__},
            )
            raise
        log.info(
            "visualize_aggregation",
            extra={
                "workflow": "visualize",
                "status": state["period"],
                "latency_ms": round((time.monotonic() - started) * 1000),
            },
        )
        overview = data["overview"]
        if overview["income_transaction_count"] + overview["expense_transaction_count"] == 0:
            period = data["period"]
            return {
                "response": {
                    "text": f"No financial data was found for {period['month']} {period['year']}."
                }
            }
        return {"visualization_data": data}

    async def analyze_visualize(self, state: WorkflowState) -> dict:
        if state.get("response"):
            return {}
        data = state["visualization_data"]
        spec = default_spec(data)
        status = "model_not_configured"
        started = time.monotonic()
        if self.analyzer is not None:
            try:
                async with asyncio.timeout(self.settings.openrouter_timeout_seconds):
                    raw = await self.analyzer(compact_payload(data), VisualizationSpec)
                spec = validate_spec(raw, data)
                status = "ok"
            except Exception as exc:
                # This boundary handles untrusted provider/validation failures only.
                # Cancellation inherits BaseException and always propagates.
                log.warning(
                    "visualize_analysis_failure",
                    extra={"workflow": "visualize", "error_type": type(exc).__name__},
                )
                status = "fallback"
        log.info(
            "visualize_analysis",
            extra={
                "workflow": "visualize",
                "status": status,
                "latency_ms": round((time.monotonic() - started) * 1000),
            },
        )
        return {"visualization_spec": spec, "visualization_analysis_status": status}

    async def render_visualize(self, state: WorkflowState) -> dict:
        if state.get("response"):
            return {}
        data, spec = state["visualization_data"], state["visualization_spec"]
        text = "\n".join(report_lines(data, spec))
        if state["visualization_analysis_status"] != "ok":
            text += "\nStandard charts; AI visualization analysis is unavailable."
        started = time.monotonic()
        try:
            # Bounded, small Pillow canvas: no browser, subprocess, temporary file,
            # background task, or unbounded rendering input to clean up.
            image = render_report(data, spec)
        except Exception as exc:
            log.warning(
                "visualize_render_failure",
                extra={"workflow": "visualize", "error_type": type(exc).__name__},
            )
            return {
                "response": {
                    "text": text
                    + "\nChart rendering is unavailable; verified totals are shown above."
                }
            }
        log.info(
            "visualize_render",
            extra={
                "workflow": "visualize",
                "status": "ok",
                "latency_ms": round((time.monotonic() - started) * 1000),
            },
        )
        return {
            "response": {
                "text": text,
                "photo_png": base64.b64encode(image).decode("ascii"),
            }
        }

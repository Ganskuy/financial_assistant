from langchain_core.tools import StructuredTool
from pydantic import Field

from app.schemas.finance import StrictSchema
from app.services.finance import FinanceService


class NoArgs(StrictSchema):
    pass


class PeriodArgs(StrictSchema):
    period: str | None = Field(default=None, pattern=r"^20\d{2}-(0[1-9]|1[0-2])$")


def safe_tools(service: FinanceService) -> dict[str, StructuredTool]:
    # Identity is captured in FinanceService, never present in model-visible args.
    async def balance() -> dict:
        return await service.current_balance()

    async def summary(period: str | None = None) -> dict:
        return await service.summary(period)

    async def categories(period: str | None = None) -> dict:
        return (await service.summary(period))["by_category"]

    async def recent() -> list:
        # Strip all untrusted text fields from the model-facing tool.
        return [
            {k: v for k, v in row.items() if k not in {"description", "merchant"}}
            for row in await service.recent()
        ]

    async def budgets(period: str | None = None) -> list:
        return await service.budgets(period)

    async def savings() -> list:
        return [{k: v for k, v in row.items() if k != "name"} for row in await service.savings()]

    async def trend(period: str | None = None) -> dict:
        return await service.trend(period)

    specs = [
        ("get_current_balance", balance, NoArgs),
        ("get_monthly_summary", summary, PeriodArgs),
        ("get_category_spending", categories, PeriodArgs),
        ("get_recent_transactions", recent, NoArgs),
        ("get_budget_status", budgets, PeriodArgs),
        ("get_savings_progress", savings, NoArgs),
        ("get_spending_trend", trend, PeriodArgs),
    ]
    return {
        name: StructuredTool.from_function(
            name=name,
            description=f"Read authenticated user's {name.removeprefix('get_').replace('_', ' ')}.",
            coroutine=coroutine,
            args_schema=schema,
        )
        for name, coroutine, schema in specs
    }

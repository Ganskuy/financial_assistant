from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

MAX_AMOUNT = 1_000_000_000_000
Money = Annotated[StrictInt, Field(ge=1, le=MAX_AMOUNT)]
NonnegativeMoney = Annotated[StrictInt, Field(ge=0, le=MAX_AMOUNT)]


class Category(StrEnum):
    FOOD = "food"
    TRANSPORT = "transport"
    HOUSING = "housing"
    UTILITIES = "utilities"
    HEALTH = "health"
    SHOPPING = "shopping"
    EDUCATION = "education"
    ENTERTAINMENT = "entertainment"
    SALARY = "salary"
    GIFT = "gift"
    OTHER = "other"


class StrictSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ReceiptItem(StrictSchema):
    name: str = Field(min_length=1, max_length=120)
    # Strings avoid loss of fractional quantities in JSON; all money remains integer IDR.
    quantity: str = Field(pattern=r"^\d{1,6}(\.\d{1,3})?$", max_length=10)
    unit_price: NonnegativeMoney
    subtotal: NonnegativeMoney

    @field_validator("quantity")
    @classmethod
    def positive_quantity(cls, value: str) -> str:
        if not Decimal(value).is_finite() or not 0 < Decimal(value) <= 100000:
            raise ValueError("Invalid quantity")
        return value


class Receipt(StrictSchema):
    items: list[ReceiptItem] = Field(max_length=60)
    subtotal: NonnegativeMoney
    discount: NonnegativeMoney
    tax: NonnegativeMoney
    service_charge: NonnegativeMoney
    grand_total: Money


class TransactionDraft(StrictSchema):
    intent: Literal["expense", "income"]
    transaction_date: date
    merchant: str | None = Field(max_length=120)
    description: str = Field(min_length=1, max_length=240)
    amount: Money
    currency: Literal["IDR"]
    category: Category
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    receipt: Receipt | None


class Extraction(StrictSchema):
    intent: Literal["expense", "income", "report", "advice", "unknown"]
    transaction: TransactionDraft | None
    period: str | None = Field(pattern=r"^20\d{2}-(0[1-9]|1[0-2])$")


class OCR(StrictSchema):
    transcription: str = Field(min_length=1, max_length=6000)
    readable: bool


class BudgetDraft(StrictSchema):
    month: date
    category: Category
    amount: Money

    @field_validator("month")
    @classmethod
    def first_day(cls, value: date) -> date:
        if value.day != 1:
            raise ValueError("Month must start on day one")
        return value


class GoalDraft(StrictSchema):
    name: str = Field(min_length=1, max_length=80)
    target: Money
    deadline: date


class SavingDraft(StrictSchema):
    name: str = Field(min_length=1, max_length=80)
    amount: Money


class AdvicePoint(StrictSchema):
    # Controlled output language deliberately excludes free prose and raw numeric values.
    action: Literal[
        "review_category",
        "reduce_optional_spending",
        "protect_surplus",
        "close_deficit",
        "review_budget",
        "build_emergency_fund",
        "track_more_data",
    ]
    fact_ids: list[str] = Field(min_length=1, max_length=3)


class Advice(StrictSchema):
    points: list[AdvicePoint] = Field(min_length=1, max_length=4)


class OpeningDraft(StrictSchema):
    amount: NonnegativeMoney
    as_of: date


class RemovalDraft(StrictSchema):
    type: Literal["expense", "income"]
    candidates: list[UUID] = Field(min_length=1, max_length=10)
    selected: UUID | None = None

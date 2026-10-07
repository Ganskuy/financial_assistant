import logging
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from app.core.errors import InvalidInput
from app.schemas.finance import MAX_AMOUNT, TransactionDraft


def today() -> date:
    return datetime.now(ZoneInfo("Asia/Jakarta")).date()


def parse_idr(raw: str) -> int:
    value = raw.strip().lower().removeprefix("rp").strip()
    match = re.fullmatch(r"(\d+(?:[.,]\d+)*)(?:\s*(ribu|rb|ratus|juta|jt|miliar))?", value)
    if not match:
        raise InvalidInput("Invalid IDR amount. Examples: 25.000, 25 ribu, 1,5 juta.")
    number, suffix = match.groups()
    if suffix:
        if number.count(",") > 1 or number.count(".") > 1 or ("," in number and "." in number):
            raise InvalidInput("Ambiguous amount; use 1,5 juta or integer IDR.")
        number = number.replace(",", ".")
    elif "." in number:
        if not re.fullmatch(r"\d{1,3}(?:\.\d{3})+", number):
            raise InvalidInput("Use dot groups of three digits for IDR.")
        number = number.replace(".", "")
    elif "," in number:
        if not re.fullmatch(r"\d+,0{1,2}", number):
            raise InvalidInput("Fractional rupiah is not supported.")
        number = number.split(",")[0]
    try:
        amount = (
            Decimal(number)
            * {
                None: 1,
                "ratus": 100,
                "rb": 1000,
                "ribu": 1000,
                "jt": 1000000,
                "juta": 1000000,
                "miliar": 1000000000,
            }[suffix]
        )
    except InvalidOperation as exc:
        raise InvalidInput("Invalid IDR amount.") from exc
    if (
        not amount.is_finite()
        or amount != amount.to_integral_value()
        or not 1 <= amount <= MAX_AMOUNT
    ):
        raise InvalidInput("Amount must be whole IDR between 1 and 1,000,000,000,000.")
    return int(amount)


def validate_transaction(draft: TransactionDraft, on_date: date | None = None) -> list[str]:
    current = on_date or today()
    valid_date = date(2000, 1, 1) <= draft.transaction_date <= current
    logging.getLogger("finance").info(
        "transaction_date_validation",
        extra={
            "transaction_date": draft.transaction_date.isoformat(),
            "current_date": current.isoformat(),
            "status": "ok" if valid_date else "rejected",
        },
    )
    if not valid_date:
        raise InvalidInput(
            "Transaction date must be between 2000-01-01 and today; future entries are not accepted. "
            f"Extracted date: {draft.transaction_date.isoformat()}; "
            f"today in Asia/Jakarta: {current.isoformat()}."
        )
    warnings = []
    if draft.confidence < 0.75:
        warnings.append("Low extraction confidence. Check every field carefully.")
    receipt = draft.receipt
    if receipt:
        if draft.intent != "expense" or receipt.grand_total != draft.amount:
            raise InvalidInput(
                "Receipt type or transaction amount does not match the receipt total."
            )
        if (
            receipt.discount > receipt.subtotal
            or receipt.tax > receipt.grand_total
            or receipt.service_charge > receipt.grand_total
        ):
            raise InvalidInput("Receipt discount, tax, or service charge is outside sane limits.")
        inconsistent = sum(
            Decimal(item.quantity) * item.unit_price != item.subtotal for item in receipt.items
        )
        if inconsistent:
            warnings.append(
                f"Quantity × unit price differs from the printed subtotal for {inconsistent} item(s)."
            )
        if receipt.items and sum(i.subtotal for i in receipt.items) != receipt.subtotal:
            warnings.append("Item subtotals do not match the stated receipt subtotal.")
        computed = receipt.subtotal - receipt.discount + receipt.tax + receipt.service_charge
        if computed != receipt.grand_total:
            warnings.append(
                f"Calculated total Rp{computed:,} differs from receipt total Rp{receipt.grand_total:,}. Confirm only after checking."
            )
    return warnings


def month_bounds(period: str | None = None) -> tuple[date, date]:
    try:
        start = date.fromisoformat(period + "-01") if period else today().replace(day=1)
    except (ValueError, TypeError) as exc:
        raise InvalidInput("Period must be YYYY-MM.") from exc
    if not 2000 <= start.year <= today().year + 1:
        raise InvalidInput("Unsupported report year.")
    end = date(start.year + (start.month == 12), start.month % 12 + 1, 1)
    return start, end


def idr(amount: int) -> str:
    return f"Rp{amount:,}"


def parse_opening_idr(raw: str) -> int:
    value = raw.strip().lower().removeprefix("rp").strip()
    if value in {"0", "0,00"}:
        return 0
    return parse_idr(raw)

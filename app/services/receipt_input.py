"""Interpret image document context without guessing account ownership or line items."""

import re
from typing import Literal

from app.core.errors import InvalidInput

DocumentKind = Literal["retail", "payment", "transfer"]
Direction = Literal["expense", "income"]
_DIGITAL = re.compile(
    r"\b(?:bukti transaksi|rincian transaksi|metode pembayaran|metode transaksi|"
    r"merchant name|acquirer name|nama acquirer|payment method|transaction details)\b",
    re.I,
)
_TRANSFER = re.compile(
    r"(?:^|\n)\s*(?:hasil transfer|bukti transfer|jumlah transfer|biaya transfer|"
    r"transfer berhasil|transfer details)\b",
    re.I,
)
_UNSETTLED = re.compile(
    r"\bstatus\s*[:\-]?\s*(?:transaksi\s+)?"
    r"(?:gagal|failed|pending|diproses|processing|dibatalkan|cancelled|menunggu)\b",
    re.I,
)


def document_kind(text: str) -> DocumentKind:
    if _TRANSFER.search(text):
        return "transfer"
    # A paper sales receipt may also print "Payment method: QRIS".
    if re.search(r"\b(?:sales receipt|struk|sub\s*total|qty|quantity)\b", text, re.I):
        return "retail"
    return "payment" if _DIGITAL.search(text) else "retail"


def caption_direction(caption: str) -> Direction | None:
    """Only an explicit leading direction label; never derive identity from names."""
    markers = re.findall(r"\b(expense|pengeluaran|income|pemasukan)\b", caption.casefold())
    directions = {"expense" if word in {"expense", "pengeluaran"} else "income" for word in markers}
    if len(directions) != 1 or not re.match(
        r"^\s*(expense|pengeluaran|income|pemasukan)\b", caption, re.I
    ):
        return None
    return directions.pop()


def image_context(text: str, caption: str = "") -> tuple[DocumentKind, Direction]:
    kind = document_kind(text)
    if kind == "transfer" and re.search(r"\b(?:rekening sendiri|own accounts?)\b", caption, re.I):
        raise InvalidInput(
            "Transfers between your own accounts are not income or expenses. No financial record was written."
        )
    if kind != "retail" and _UNSETTLED.search(text):
        raise InvalidInput(
            "This screenshot shows an incomplete, failed, or cancelled payment. "
            "Please send its successful confirmation. No financial record was written."
        )
    direction = caption_direction(caption)
    if kind == "transfer" and direction is None:
        raise InvalidInput(
            "The transfer is readable, but I cannot tell whether you sent or received it. "
            "Resend it with caption 'Pengeluaran' (you sent money) or 'Pemasukan' "
            "(you received money). Transfers between your own accounts are neither income "
            "nor expenses. No financial record was written."
        )
    return kind, (direction or "expense") if kind != "retail" else "expense"


def payment_amounts(text: str, *, direction: Direction) -> set[int]:
    """Prefer explicitly labeled totals; never parse identifiers as monetary evidence."""

    def amounts(pattern: str) -> set[int]:
        values = set()
        for match in re.finditer(pattern, text, re.I):
            number = match.group(1)
            # Whole IDR: dot/comma thousands groups, optional Indonesian ,00 decimals.
            if re.fullmatch(r"\d{1,3}(?:\.\d{3})+(?:,00)?", number):
                value = number.removesuffix(",00").replace(".", "")
            elif re.fullmatch(r"\d{1,3}(?:,\d{3})+", number):
                value = number.replace(",", "")
            elif re.fullmatch(r"\d+(?:,00)?", number):
                value = number.removesuffix(",00")
            else:
                continue
            values.add(int(value))
        return values

    # Do not include a sender's fee in an explicitly incoming transfer.
    label = (
        r"(?:jumlah transfer|amount received|jumlah diterima)"
        if direction == "income"
        else r"(?:jumlah total|grand total|total(?: paid)?)"
    )
    totals = amounts(r"\b" + label + r"\s*[:=]?\s*(?:rp\.?\s*)?(\d[\d.,]*)(?!\w)")
    return totals or amounts(r"\brp\.?\s*(\d[\d.,]*)(?!\w)")

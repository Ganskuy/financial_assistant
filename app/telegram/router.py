import re
from dataclasses import dataclass

from app.services.validation import today

MONTHS = {
    "januari": 1,
    "january": 1,
    "februari": 2,
    "february": 2,
    "maret": 3,
    "march": 3,
    "april": 4,
    "mei": 5,
    "may": 5,
    "juni": 6,
    "june": 6,
    "juli": 7,
    "july": 7,
    "agustus": 8,
    "august": 8,
    "september": 9,
    "oktober": 10,
    "october": 10,
    "november": 11,
    "desember": 12,
    "december": 12,
}
COMMANDS = {
    "balance",
    "remove",
    "opening",
    "history",
    "usage",
    "help",
    "start",
    "report",
    "budget",
    "savings",
    "goal",
    "save",
    "pending",
    "advice",
    "visualize",
}


@dataclass(frozen=True)
class Route:
    name: str
    args: str = ""
    period: str | None = None


def period_from_text(text: str) -> str | None:
    explicit = re.search(r"\b(20\d{2}-(?:0[1-9]|1[0-2]))\b", text)
    if explicit:
        return explicit[1]
    current = today()
    if "bulan lalu" in text or "last month" in text:
        month = current.month - 1 or 12
        return f"{current.year - (current.month == 1):04d}-{month:02d}"
    year = re.search(r"\b(20\d{2})\b", text)
    for word in re.findall(r"[a-z]+", text):
        if word in MONTHS:
            return f"{int(year[1]) if year else current.year:04d}-{MONTHS[word]:02d}"
    return None


def route_text(text: str) -> Route:
    text = text.strip()
    if text.startswith(("/", "\\")):
        command, _, args = text.partition(" ")
        name = command[1:].split("@")[0].lower()
        return Route(name if name in COMMANDS else "unsupported", args.strip())
    lowered = text.lower()
    # Fully anchored statements only: never infer an opening amount from an income,
    # expense, hypothetical, or a message containing several financial instructions.
    opening = re.fullmatch(
        r"(?:(?:saat ini )?(?:aku|saya) (?:memiliki|punya) uang(?: (?:sekarang|saat ini))?(?: sebanyak| sebesar)?|saldo awal(?: saya)?|uang saya sekarang|i (?:currently have|have))"
        r"\s+(?:rp\.?\s*)?(\d+(?:[.,]\d+)*(?:\s*(?:ribu|rb|juta|jt|miliar))?)(?:\s*(?:rupiah|idr))?[.!]?",
        lowered,
    )
    if opening:
        return Route("opening", args=opening[1])
    if re.fullmatch(r"(cek |lihat )?(saldo|balance)( saya)?[?.!]?", lowered):
        return Route("balance")
    if re.match(
        r"^(bagaimana|gimana|buat(kan)?|tampilkan|kategori apa|how|give|show|analisis|analyze|saran|advice)\b",
        lowered,
    ) and re.search(
        r"pengeluaran|keuangan|laporan|boros|spending|report|financial|hemat|advice|saran", lowered
    ):
        return Route("advice", period=period_from_text(lowered))
    return Route("extract")

"""Bounded date-only observations; never log receipt text or identifiers."""

import re

_DATE = re.compile(
    r"(?<!\w)(?:\d{4}-\d{2}-\d{2}|\d{1,2}[/.-]\d{1,2}[/.-]\d{4}|"
    r"\d{1,2}\s+(?:Jan(?:uary|uari)?|Feb(?:ruary|ruari)?|Mar(?:ch|et)?|"
    r"Apr(?:il)?|May|Mei|Jun[ei]?|Jul[yi]?|Aug(?:ust)?|Agu(?:stus)?|"
    r"Sep(?:tember)?|Oct(?:ober)?|Okt(?:ober)?|Nov(?:ember)?|Dec(?:ember)?|"
    r"Des(?:ember)?)\s+\d{4})(?!\w)",
    re.IGNORECASE,
)


def receipt_date_candidates(text: str) -> list[str]:
    """Return date-shaped strings, not authoritative parsed transaction dates."""
    return list(dict.fromkeys(" ".join(m.group().split()) for m in _DATE.finditer(text)))[:8]

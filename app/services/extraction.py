"""Small, conservative semantic checks after structured extraction.

These checks ground merchant text, not real-world merchant identity. They never
change financial amounts, dates, intent, or category, and do not call a model.
"""

import re
import unicodedata

from app.schemas.finance import TransactionDraft

_GENERIC_BUSINESSES = (
    "spbu",
    "warung",
    "bengkel",
    "minimarket",
    "supermarket",
    "apotek",
    "restoran",
    "kedai",
    "toko",
    "pasar",
    "laundry",
    "barbershop",
)
_NON_BUSINESSES = {"rumah", "kantor", "sekolah", "home", "office", "school"}
_AMBIGUITY = re.compile(
    r"\b(?:kalau|jika|seandainya|mungkin|rencana|mau|akan|lupa|atau|"
    r"if|maybe|perhaps|plan|planning|would|might|or)\b"
)
_CONTEXT_RELATION = re.compile(
    r"\b(?:ke|menuju|dekat|depan|samping|sebelah|near|toward|towards|to|"
    r"past|behind|outside|melewati|lewat|sekitar)\s+(?:the\s+)?$"
)
_PRODUCT_RELATION = re.compile(r"\b(?:beli|membeli|buy|bought)\s+$")
_PURCHASE = re.compile(
    r"\b(?:beli|membeli|belanja|bayar|membayar|makan|minum|ngopi|isi|"
    r"tambal|menambal|servis|potong|cuci|nyuci|buy|bought|paid|repair|repaired|fix|fixed)\b"
)
_TRANSFER = re.compile(r"\b(?:transfer|kirim|mengirim|sent|send|gave|titip)\b")
_PURPOSE = re.compile(
    r"\b(?:buat|untuk|supaya|agar|to)\s+(?:beli|membeli|buy|tambal|menambal|repair|isi)\b"
)
_NEGATION = re.compile(r"\b(?:bukan|tidak|nggak|gak|batal|belum|not|never)\b")
_GENERIC_AT = re.compile(
    r"\b(?:di|at)\s+(?:(?:a|an|the|sebuah)\s+)?("
    + "|".join(_GENERIC_BUSINESSES)
    + r")(?=$|\s+(?:rp\s*\d|idr\b|\d|seharga\b|sebesar\b|harga\b|for\b|"
    r"dekat\b|near\b|tadi\b|kemarin\b))"
)


def _canonical(value: str) -> str:
    return " ".join(re.findall(r"\w+", unicodedata.normalize("NFKC", value).casefold()))


def _merchant_supported(merchant: str, source_text: str, *, receipt: bool) -> tuple[bool, str]:
    phrase = _canonical(merchant)
    if not phrase:
        return False, "merchant_unsupported"
    pattern = re.compile(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)")
    text = _canonical(source_text)
    if not pattern.search(text):
        return False, "merchant_unsupported"
    if receipt:
        return True, ""
    if phrase in _NON_BUSINESSES:
        return False, "merchant_role_ambiguous"

    # Scope uncertainty to the sentence containing the merchant; an unrelated
    # future plan in a following sentence does not invalidate a past purchase.
    contexts = [
        _canonical(sentence)
        for sentence in re.split(r"[.!?;\n](?:\s+|$)", source_text)
        if pattern.search(_canonical(sentence))
    ] or [text]
    for context in contexts:
        if _AMBIGUITY.search(context) or (_TRANSFER.search(context) and _PURPOSE.search(context)):
            continue
        for occurrence in pattern.finditer(context):
            prefix = context[: occurrence.start()]
            # Reject demonstrated contextual/product roles; keep supported named
            # merchant shorthand instead of requiring one specific preposition.
            if _CONTEXT_RELATION.search(prefix) or re.search(
                r"\b(?:bukan|not)(?:\s+\w+){0,2}\s+$", prefix
            ):
                continue
            if not _PRODUCT_RELATION.search(prefix):
                return True, ""
    return False, "merchant_role_ambiguous"


def _explicit_generic(text: str, intent: str) -> str | None:
    # Restore only a narrow, explicit purchase relation. Broader interpretation
    # belongs to the model; no merchant is safer than guessing a name or brand.
    if (
        intent != "expense"
        or _AMBIGUITY.search(text)
        or _TRANSFER.search(text)
        or _PURPOSE.search(text)
        or _NEGATION.search(text)
        or len(re.findall(r"\b(?:di|at)\b", text)) != 1
    ):
        return None
    matches = list(_GENERIC_AT.finditer(text))
    candidates = {
        match.group(1)
        for match in matches
        if _PURCHASE.search(text[: match.start()])
        and not re.search(r"\b(?:bukan|not)(?:\s+\w+){0,2}\s+$", text[: match.start()])
    }
    if len(candidates) != 1:
        return None
    candidate = candidates.pop()
    return "SPBU" if candidate == "spbu" else candidate


def ground_transaction(
    draft: TransactionDraft, source_text: str, *, receipt: bool = False
) -> tuple[TransactionDraft, list[str]]:
    """Return a grounded copy plus bounded reason codes safe for logs/previews.

    Optional unknown merchants remain valid and never trigger a model retry.
    Receipt grounding trusts only the OCR transcription's literal text; it cannot
    verify OCR recognition or distinguish every printed product from a merchant.
    Manual text excludes demonstrated product, destination, nearby-landmark and
    uncertainty contexts while preserving supported named merchant shorthand.
    """
    text = _canonical(source_text)
    merchant = draft.merchant
    reasons: list[str] = []
    if merchant is not None:
        supported, reason = _merchant_supported(merchant, source_text, receipt=receipt)
        if not supported:
            merchant = None
            reasons.append(reason)
    if merchant is None and not receipt:
        merchant = _explicit_generic(text, draft.intent)
        if merchant is not None:
            reasons.append("merchant_generic_restored")

    description = draft.description
    if merchant and not receipt:
        cleaned = re.sub(
            r"\s+(?:di|at|dari|from)\s+(?:(?:a|an|the|sebuah)\s+)?"
            + re.escape(merchant)
            + r"[.!]?$",
            "",
            description,
            flags=re.IGNORECASE,
        ).strip()
        if cleaned and cleaned != description:
            description = cleaned
            reasons.append("description_merchant_removed")
    if not reasons:
        return draft, []
    return draft.model_copy(update={"merchant": merchant, "description": description}), reasons

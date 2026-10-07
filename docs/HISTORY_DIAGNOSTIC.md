# Receipt visibility in history — 7 October 2026

Read-only inspection of the local database found four confirmed receipt entries
and ten confirmed text entries. No confirmed transaction preview lacked its
corresponding ledger record. One additional receipt preview had been cancelled.
No user records were modified during the investigation.

The original query included both input sources but sorted by transaction date
before creation time and returned only ten rows. The two most recently saved
receipts appeared at positions seven and eight because their purchase date was
earlier than that of six text entries. An older FamilyMart receipt was at position
twelve and was excluded by the limit. The response also omitted merchant names.

This establishes an ordering, identification, and older-history accessibility
problem, not an OCR persistence failure. The newest receipts were included in the
query at inspection time; the exact earlier Telegram response was unavailable, so
this investigation does not prove that response omitted them. Telegram's sender
splits long responses rather than truncating them. History has no receipt-specific
filter, cache, or duplicate suppression.

## Fix

- Sort by creation time with an ID tie-breaker, so newly saved OCR and text entries
  appear first while retaining their actual purchase dates.
- Display the merchant/source alongside the description.
- Add `/history PAGE`, with ten entries per page and next/previous commands.
  Fetch one extra row to detect a following page without an extra count query.
- Preserve confirmation requirements, per-user isolation, and duplicate warnings.
- Exclude the newly exposed merchant field from the model-facing recent-entry tool,
  just as descriptions are excluded.

## Verification

All 197 tests passed against a disposable PostgreSQL database, which was migrated
and removed after testing. The new regression drives a photo through the receipt
graph with deterministic model outputs, confirms it, and checks the actual bot's
history responses. It covers older purchase dates, merchant visibility, pagination
without missing or repeated rows in a stable dataset, pending exclusion, duplicate
warnings, user isolation, empty pages, invalid page arguments, and zero model calls
for history. A separate test checks merchant/description removal from model input.

Page offsets can shift when another transaction is confirmed between page requests;
start again at `/history` to refresh. No historical dates or transaction values are
rewritten. No live model calls or Telegram sends were needed for this fix.

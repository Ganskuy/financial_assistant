# Digital payment images — October 10, 2026

## Confirmed issue and scope

The previous receipt extraction contract required every image to produce an expense
with an itemized receipt object. A schema-valid QRIS/payment extraction with
`receipt=null` was rejected by that backend contract. Bank/wallet confirmations
often have no purchased items to substantiate such an object. Telegram image
captions were also discarded, preventing an explicit transfer direction from
reaching extraction.

All six user-supplied examples (three previously rejected and three accepted)
decoded and normalized locally. The rejected examples include bank transfer,
QRIS and dark wallet confirmations; accepted examples also include a dark wallet
screen. This does not establish their actual provider failure stage. No new live
OpenRouter evaluation was performed: external submission of these private images
requires destination-specific authorization. No claim of improved live accuracy,
latency or cost is made.

## Implementation

- OCR prompt `receipt-ocr-v2` explicitly accepts digital confirmations, including
  dark screens without purchased items, and preserves their field labels.
- A lightweight classifier recognizes known payment/transfer labels. Itemized
  retail receipts retain their existing contract. Unknown layouts retain that
  strict path rather than automatically relaxing every receipt.
- Digital extraction adds `payment-image-v1`: use the explicit payment/received
  amount, payee/sender instead of the app/acquirer, printed date, and a neutral
  purpose unless supported. It uses `receipt=null`; generated itemization is
  removed before validation and staging.
- A transfer requires an explicit leading caption `Pengeluaran`/`expense` or
  `Pemasukan`/`income`. Missing/conflicting directions stop after OCR with a
  clarification. Ownership is never inferred from personal names. Recognized
  own-account captions and unsuccessful status labels also stop safely.
- Captions remain bounded, separately labeled untrusted data. Retail receipts
  remain expense-only. The JSON envelope size limit accommodates the caption
  without increasing the source text limit.
- A deterministic monetary evidence check prefers labeled totals for expenses
  and transfer principal for income; it otherwise checks visible Rp-prefixed
  amounts. It excludes transaction identifiers and rejects mismatching extracted
  amounts through the existing bounded recovery path.
- Safe model logs identify the OCR/payment prompt versions without logging
  private images, captions or OCR text.

## Compatibility and efficiency

GPT-5.4 Nano, model parameters, 1024-pixel image normalization, high image detail,
database schema and LangGraph topology are unchanged. No extra model, OCR service,
dependency, image processing pass or normal-path request was added. Valid images
still use two calls (OCR then extraction); one semantic recovery is shared by
those stages, with separately bounded transport retries and global reservations.
All financial writes still require confirmation and remain idempotent.

No larger-model fallback or contrast/crop retry was added: the confirmed backend
contract rejection is independent of model size. Image enhancement or model
escalation should follow measured recognition failures, not this contract error.

## Verification

The existing 343-test suite passed before these additions. The updated suite
passed 379 tests against an isolated PostgreSQL 16 database, with external calls
mocked. New coverage includes paper/digital routing, explicit direction, pending
and failed statuses, total/fee grounding, one-retry exhaustion, removing invented
itemization, caption propagation, confirmation-only persistence and duplicate
delivery/confirmation. Private examples and raw OCR are not repository fixtures.

Additional live model tokens used for this investigation: **0** (25,000 maximum
authorized task allowance). Local tests do not prove recognition accuracy on the
six real examples or production Telegram delivery.

## Remaining limitations

Layout detection and monetary evidence parsing cover explicit Indonesian/English
labels, not every bank format or currency notation. They cannot repair incorrect
OCR, guarantee every merchant role, or derive account ownership. If no supported
monetary notation is found, existing schema/business validation and confirmation
remain the safeguards; the evidence check is not a full financial parser. Small
print can still be lost during downscaling. A controlled, no-ledger-write paired
live evaluation is still needed before attributing the reported failures to OCR
or measuring an accuracy improvement.

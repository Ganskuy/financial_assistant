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
screen. The authorized Nano evaluation subsequently read all six images. Two previously
rejected digital payments passed extraction and validation, with exact amount and
date matches; replaying those outputs against the legacy contract rejects both.
This confirms the contract incompatibility but does not reconstruct the original
failed provider responses. No general accuracy, latency or cost improvement is claimed.

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

Authorized live evaluation used **13,171 tokens across 10 provider requests**,
with no outstanding task reservations or financial writes. The persistent task
guard checks a conservative full request reservation against the 25,000-token
ceiling before dispatch; existing global PostgreSQL accounting remains active.

| Case | Live outcome | Tokens | Elapsed seconds |
| --- | --- | ---: | ---: |
| u1: transfer | Readable; safely asks for direction | 988 | 2.92 |
| u2: QRIS payment | Validated; amount/date match | 2,728 | 4.64 |
| u3: wallet payment | Validated; amount/date match | 3,016 | 3.42 |
| a1: paper receipt | Validated; amount/date match | 2,729 | 6.42 |
| a2: paper receipt | Validated; amount/date match | 2,671 | 4.82 |
| a3: phone payment | OCR passed; extraction blocked before dispatch by token guard | 1,039 | 2.94 |

The four completed extractions passed without recovery. Their amounts and dates
matched the inspected images (4/4); this is a small sample, not an overall accuracy
benchmark. All six OCR calls returned readable output. The transfer clarification
is expected behavior, not an OCR failure. The final extraction was not attempted:
its conservative reservation exceeded the remaining 11,829-token task allowance.
No model settings or accounting rules were relaxed to fit another call. Latencies
are single-run wall times, not a controlled before/after comparison. See the
[sanitized evaluation record](evaluation/digital-payment-live.json).

## Remaining limitations

Layout detection and monetary evidence parsing cover explicit Indonesian/English
labels, not every bank format or currency notation. They cannot repair incorrect
OCR, guarantee every merchant role, or derive account ownership. If no supported
monetary notation is found, existing schema/business validation and confirmation
remain the safeguards; the evidence check is not a full financial parser. Small
print can still be lost during downscaling. The last accepted example still needs
full live extraction verification, and transfer direction must be supplied by the
user. A paired live baseline and repeated trials are still needed to measure an
accuracy improvement; production Telegram delivery was not tested.

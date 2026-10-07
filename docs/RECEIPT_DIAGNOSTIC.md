# Receipt date diagnostic — 7 October 2026

The local live diagnostic reproduced the reported date rejection using
`~/Downloads/famima.jpeg`, production image normalization, configured OpenRouter
models, and the normal OCR/extraction/validation nodes. It never invoked staging,
confirmation, ledger writes, or Telegram sending. API usage was accounted for in
the existing daily token budget.

## Observed failure

At 07:15 UTC (14:15 Jakarta), the observations were:

| Stage | Value |
| --- | --- |
| Date-shaped text in OCR transcription | `06/10/2026` |
| Parsed extraction transaction date | `0610-10-07` |
| Current Jakarta date | `2026-10-07` |
| Validation | Rejected: before 2000-01-01 |

The OCR handoff preserves transcription text, JSON encodes it, and sends it to a
second model. It does not rearrange dates. Pydantic parses the model's transaction
date before the business validator checks the supported range. The reproduced
failure is therefore downstream of correct OCR, at extraction/schema parsing,
not duplicate detection or a future-dated receipt. The original Telegram failures
cannot be retrospectively proven identical: completed jobs discard payloads and
responses and the old logs did not record dates.

The failing call's raw JSON field was not captured. The diagnostic now also prints
the raw model date (only if it has ISO date shape) and its type to distinguish
provider output from parsing. Three subsequent runs of the original prompt all
returned raw string `2026-10-06` and passed; the error is intermittent.

## Changes and verification

- Added explicit Indonesian DD/MM/YYYY conversion instructions, exact printed-year
  preservation, and instructions not to combine receipt and current-date parts.
  Extraction logs identify this prompt as `receipt-date-v2`.
- Added bounded date-only OCR observations, extracted dates, and date-validation
  outcomes to allowlisted structured logs, correlated with the existing request ID.
  These observations do not parse or override financial dates.
- Date rejection messages now include both compared dates. Validations remain strict.
- Added graph regression coverage for the observed year 0610, before-2000 dates,
  future dates, today, yesterday, both receipt date formats, unreadable OCR, and
  invalid/missing extraction. Invalid paths must never reach staging.
- Three live runs after the prompt adjustment returned `2026-10-06` and passed.
  This is a small smoke sample, not proof that model extraction cannot fail again.
- The user subsequently verified successful extraction and the similar-transaction
  warning through the running Telegram bot.
- Final publication checks: all 195 tests passed, including PostgreSQL integration
  tests against a newly created disposable database. Alembic upgrade and schema
  drift checks passed; the database was removed afterward. Dependency consistency,
  Ruff lint, formatting, and Git whitespace checks passed.

Seven live runs in this investigation consumed 14,936 tokens in total. No financial
transactions were created. Raw receipt text, phone numbers, and transaction IDs
were not persisted in diagnostic logs.

## Reproduce

From the project root with the local environment and database available:

```sh
envir/bin/python -m scripts.diagnose_receipt ~/Downloads/famima.jpeg --runs 3
```

The script stops on the first safe workflow failure, supports at most five runs,
and uses real provider calls and normal token-budget enforcement. The budget delta
is global: concurrent bot traffic can contribute to it. Exit status is nonzero on
failure. Only accounting records are written.

Reload/restart the bot to load the code changes, then correlate
`receipt_ocr_dates`, `transaction_extracted_date`, and
`transaction_date_validation` using its `tg-...` request ID.

## Remaining boundaries

- Telegram recompression and the exact historical Telegram image bytes were not
  replayed. The local file went through the same backend normalization function.
- Running processes were located in this checkout; their in-memory settings and
  exact loaded source versions were not inspected.
- A date within the accepted range can still be semantically wrong. There is no
  deterministic cross-check against a uniquely identified printed transaction date.
  Do not silently replace incorrect dates with today; retain confirmation review.
- OCR date observations recognize common numeric and English/Indonesian month
  formats, but are not a complete date parser. An empty list does not prove the
  receipt lacked a date, and multiple candidates may refer to different events.

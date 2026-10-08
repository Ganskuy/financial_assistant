# Extraction reliability changes — 2026-10-08

This is an incremental change to transaction extraction. Both vision and extraction still use `openai/gpt-5.4-nano`, with the existing output caps, high image detail, and reasoning configuration. The advisor, graph topology, database schema, confirmation contract, environment, deployment configuration, and dependencies are unchanged.

## Findings and changes

| Boundary | Confirmed finding | Implemented change |
| --- | --- | --- |
| Semantic extraction | The existing prompt did not distinguish a named seller, generic business, product, destination, or landmark. The paired evaluation reproduced a missing SPBU merchant. | Concise `extraction-v3` field definitions and examples; explicit generic locations are permitted, unsupported brand inference is forbidden, and descriptions avoid redundant merchant clauses. |
| Merchant validation | Schema validity alone could accept a merchant absent from the input. | Lightweight grounding against original text or OCR, case/Unicode/punctuation normalization, contextual rejection, and narrowly scoped restoration of explicit generic business names. Optional unknown merchants stay null without a retry. |
| Image normalization | Direct alpha removal destroyed black text on a transparent PNG canvas before any model request. | Selective white-background compositing for transparent inputs, with regression tests. Existing EXIF correction, JPEG normalization, 1024-pixel envelope, 20-million-pixel safety cap, and file-size bounds remain. |
| Telegram documents | A generic declared binary MIME was rejected even though the download path already accepted it and decoded image bytes independently. | Permit `application/octet-stream` declarations and normalize MIME case/parameters; actual decoded bytes must still be a single-frame JPEG or PNG. Largest-photo selection already worked and is covered by routing tests. |
| Input envelope | A 6000-character transcription could exceed the client's 6000-character limit after JSON wrapping and escaping. | Keep source text capped at 6000; allow its bounded serialized envelope for the extraction role only. |
| Model output | Null/list message envelopes could raise an uncaught exception; refusal was not explicitly distinguished. | Validate message shape and handle refusal, content filtering, empty output, truncation, invalid JSON/schema, unexpected tools, and missing usage safely. |
| Recovery | Any malformed structured result ended extraction immediately. | At most one targeted structured-output recovery shared across OCR and extraction. No graph loop, new model, OCR service, or background infrastructure. |
| Rate limits | `Retry-After` was truncated to five seconds, potentially retrying earlier than requested. | Parse seconds or HTTP dates. Delays up to five seconds are honored with existing backoff/jitter; longer guidance ends safely so the user can try later. HTTP 408 joins existing retryable 429/5xx/timeouts. |
| Observability | Successful transport did not identify later semantic/business rejection. | Safe stage/reason, attempt, correction, preprocessing, and latency fields alongside existing request IDs, token usage and provider cost. No raw text, images, merchant names, credentials, or provider exception bodies are added to logs. |

The transparency defect is reproducible, but it does **not** establish the cause of every previously failing receipt. The available real receipt is JPEG. Telegram compression, small printed text, model nondeterminism, and other receipt layouts remain possible causes requiring corresponding failing samples. The real receipt passed the current and historical boundaries in the completed checks.

## Validation and retry rules

1. Telegram metadata, file path, download byte limit, MIME, decoded image format/frame count, pixel limit and corruption checks run before vision. Invalid images do not reach the model.
2. Strict JSON Schema and Pydantic remain the structural boundary. Integer IDR limits, enums, nullable fields and decimal-string item quantities are unchanged. The existing confidence field is retained for compatibility; it is not newly calibrated or used to trigger retries.
3. An invalid/incomplete structured response may consume one extra attempt with the original evidence plus concise repair instructions. OCR and extraction share that single allowance. Invalid generated financial values are never fed back as evidence.
4. Refusal/content filtering, missing usage accounting, invalid input, unknown intent, unreadable images and absent optional merchants do not trigger semantic retries. Schema-valid intent mismatches or missing required receipt structure share the same one-retry allowance; repeated contract errors and business-rule errors stop safely instead of guessing a correction. Unreadable receipts request an original JPEG/PNG document, sharper photo, or explicit text.
5. Existing HTTP retries remain separate: at most two HTTP attempts per structured request, each requiring its own durable token reservation. A normal text transaction uses one request; a normal receipt uses OCR plus extraction (two requests). With recovery, maximum structured requests are two for text and three for receipts; with transport retries, maximum HTTP attempts are four and six respectively. Global budget limits can stop earlier.
6. Business validation still rejects invalid dates, incompatible receipt intent/amount, and out-of-range receipt fields. Existing arithmetic discrepancies remain visible warnings at confirmation rather than silently changing printed totals. No schema or model output is treated as an authorized financial write.
7. Staging/confirmation, update/message uniqueness, durable responses and ledger uniqueness remain unchanged. Reprocessing a staged update skips extraction; delivery retries and repeated confirmation cannot create duplicate ledger records.

## Verification and measured results

The original commit `34b136a` passed **197 tests** against a disposable PostgreSQL database. The final suite includes semantic, ingestion, routing, malformed/refused output, transport/recovery, evaluator, security, workflow and persistence tests. See the final verification entries below and the sanitized paired reports under `docs/evaluation/`.

Live evaluations stop at extraction and business validation. They use the configured shared PostgreSQL token budget, but do not call staging, confirmation, Telegram send, or ledger mutation methods. Text labels and receipt labels were fixed before their respective runs. Both variants use the same installed transport/schema/validator; historical prompts, OCR/extract methods and image normalization are loaded from the original Git revision. This is a boundary comparison, not a full historical deployment benchmark.

Description scoring is exact matching against predeclared alternatives, not a calibrated semantic assessment; valid unlisted paraphrases can fail. One real receipt cannot establish a general receipt success rate. Small-sample latency percentiles and cost differences are observations, not performance guarantees. Image-only CPU/RSS measurements are documented separately in [the image benchmark](RECEIPT_IMAGE_BENCHMARK.md).

## Files and reproducibility

- `app/agents/prompts/system.py`, `app/services/extraction.py`: prompt and deterministic merchant semantics.
- `app/agents/nodes/finance.py`, `app/agents/state.py`: bounded recovery and boundary checks within existing nodes.
- `app/llm/client.py`, `app/core/errors.py`: response classification, envelope limit, and transport handling.
- `app/telegram/client.py`: transparent PNG correction, MIME handling and ingestion diagnostics.
- `app/core/logging.py`, `app/services/bot.py`: safe event fields and workflow duration.
- `scripts/evaluate_extraction.py`, `tests/evals/extraction_cases.json`: reusable opt-in evaluation, 26 labeled cases, no financial writes.
- New/extended `tests/unit/` and `tests/evals/` coverage plus existing PostgreSQL integration tests.

See [evaluation commands, scoring and limitations](EXTRACTION_EVALUATION.md). Structured output settings remain consistent with the existing [OpenRouter JSON Schema interface](https://openrouter.ai/docs/guides/features/structured-outputs); no SDK migration was needed. Transport behavior follows the documented [OpenRouter error and retry guidance](https://openrouter.ai/docs/api-reference/errors-and-debugging).

## Remaining limitations

- A receipt still uses two normal model requests to preserve the existing OCR → extraction graph; combining them would change this established boundary.
- No additional crop, contrast enhancement, upscaling, new detail setting, or resolution increase was introduced without evidence. Original document upload avoids Telegram photo compression, but the existing bounded normalization still applies.
- Grounding can eliminate unsupported names and demonstrated role errors; it cannot prove a merchant's identity, correct arbitrary OCR mistakes, or interpret every linguistic ambiguity. Receipt grounding relies on literal OCR content and cannot distinguish every product from a seller.
- Amounts, dates and receipt arithmetic remain validated by the existing schema/business rules; merchant correction never invents or edits them. Broader semantic validation still depends on the model, the available evidence, and the user's confirmation.
- Production Telegram delivery/webhook acceptance and a broad held-out real receipt corpus were not exercised. No live financial records were created for acceptance testing.

## Completed verification record

- Baseline full suite: **197 passed**. Final full suite: **338 passed**, including all PostgreSQL integration tests (no skips).
- One intermediate integration run shared its disposable database with the runtime smoke-check worker and failed due to that interference. After stopping that worker, the fully isolated rerun passed all 338 tests; readiness now uses a separate disposable database.
- Ruff lint/format, `pip check`, migration upgrade and `alembic check`: passed. No migration or dependency change was required.
- All 63 pinned dependencies match the existing Docker dependency image. Building the final application on those verified cached dependencies and running as UID 10001 succeeded; `/health` and `/ready` returned HTTP 200.
- A clean build from the repository Dockerfile was attempted but failed during the unchanged dependency download step with a `files.pythonhosted.org` read timeout. This is an unresolved clean-build verification limitation, not a passing cold build.
- Largest Telegram photo selection, original-document MIME handling, download failure before model invocation, unchanged graph paths, recovery limit, staging, duplicate submissions, repeated confirmation, and delivery retry behavior are covered by mocked/unit and isolated PostgreSQL integration tests. Actual Telegram delivery was not exercised.

### Live observations (small samples, not a general accuracy benchmark)

All runs used GPT-5.4 Nano with the same configured model settings and existing token accounting. Reports preserve per-case failures; labels were not changed after observing results.

| Metric | Initial paired baseline → updated (12 cases each) | Second paired baseline → updated (12 cases each, before final contract recovery) |
| --- | --- | --- |
| Amount exact match | 11/11 → 11/11 | 11/11 → 10/11 |
| Merchant exact match (including one negative case) | 11/12 → 12/12 | 11/12 → 12/12 |
| Description wording match | 6/11 → 10/11 | 7/11 → 9/11 |
| All labeled fields / first-pass success | 7/12 → 11/12 | 8/12 → 10/12 |
| Structured output validity | 12/12 → 12/12 | 12/12 → 12/12 |
| Hallucinated merchants / unsupported negative-case transactions | 0/12 → 0/12 | 0/12 → 0/12 |
| Retry frequency | 0/12 → 0/12 | 0/12 → 0/12 |
| p50 / p95 wall time (milliseconds) | 1464 / 2523 → 1380 / 1621 | 1462 / 2352 → 1403 / 1817 |
| Input / output tokens | 10176 / 882 → 15024 / 873 | 10176 / 878 → 15036 / 825 |
| Provider cost for the 12-case workload (USD) | 0.003138 → 0.004096 | 0.003133 → 0.004038 |
| Cost per all-fields-matching transaction, including workload overhead (USD) | 0.000523 → 0.000410 | 0.000448 → 0.000449 |

The second comparison exposed an intermittent schema-valid intent-contract failure on the transfer example. It was safely rejected, but reduced successful amount extraction. This confirmed gap led to extending the *existing* single recovery allowance to inconsistent intents/missing required transaction structure. Five additional deterministic tests verify recovery success, repeated failure, and the shared OCR/extraction limit. The **final targeted live check passed 3/3** for Indonesian SPBU, English SPBU, and the transfer negative-merchant example: amount, merchant, description, schema, and business checks all passed without retries. A full 12-case comparison was not repeated after that final recovery change; the final live check does not establish that nondeterministic contract failures are eliminated. The shared daily token budget was retained throughout.

The initial report used an earlier prompt version label and stricter shorthand grounding; the second paired report and final targeted report use the final `extraction-v3` prompt. These differences and the source/prompt hashes must be considered when comparing reports. No labels were tuned to observed paraphrases: the landmark description remained outside the accepted alternatives, and is reported as a wording mismatch rather than a proven financial error.

The one available real JPEG receipt was evaluated **twice per variant**. Both variants passed 2/2 on amount, merchant, date and validation, with no retries. Description was deliberately unlabeled before execution. Baseline → updated p50 was 6410 → 5805 ms; workload cost was $0.001924 → $0.001976. This is preservation on one receipt, not evidence of improved population-level OCR accuracy. Its image, printed identifiers and merchant values are not committed. The sanitized report uses an opaque case ID.

The longer semantic prompt increases input tokens and per-request provider cost; no token-cost reduction is claimed. These tiny latency samples are too small to promise a speed improvement. The image benchmark shows similar ordinary-JPEG CPU/RSS and a small selective transparency-processing overhead.

Reports: [initial paired](evaluation/initial-paired.json), [paired before contract recovery](evaluation/paired-before-contract-recovery.json), [final targeted](evaluation/final-targeted.json), [private-receipt metrics](evaluation/real-receipt-paired.json).

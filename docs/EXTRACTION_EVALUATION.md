# Transaction extraction evaluation

`scripts/evaluate_extraction.py` compares the historical extraction boundary with the current workflow using the existing GPT-5.4 Nano vision and extraction profiles. It never stages pending transactions, confirms transactions, runs callbacks, or writes to the financial ledger. Live runs use the application's PostgreSQL token reservations and accounting, so they consume the shared daily allowance and incur provider charges.

The checked-in dataset has 26 labeled cases: 20 text cases and 6 synthetic receipt transcriptions. It includes the reported SPBU example, explicit and generic merchants, unknown merchants, destinations and landmarks that are not merchants, product names, transfer context, Indonesian amounts, quantities, missing amounts, prompt injection, dates, discounts, taxes, and printed totals that disagree with arithmetic. Provenance is recorded per case. Transcription cases test extraction **after OCR**; they do not establish image recognition accuracy.

## Run without a provider

From the project root, using the existing virtual environment:

```bash
envir/bin/python -m scripts.evaluate_extraction
envir/bin/pytest -q tests/evals/test_extraction_evaluator.py
```

The first command validates the labels and prints dataset counts without loading credentials, connecting to PostgreSQL, or calling a model. Unit tests use mocks and exercise scoring, unavailable usage metadata, safe output, image flow, and exclusion of financial write methods.

## Paired live evaluation

Start the configured PostgreSQL service and apply the existing migrations first. Use the same environment and model settings for both variants. Existing budget enforcement remains active; the evaluator refuses non-Nano extraction or vision configuration instead of changing configuration.

```bash
envir/bin/python -m scripts.evaluate_extraction \
  --live --variant both --runs 1 \
  --case-ids generic_spbu_id,generic_spbu_en,named_merchant,unknown_merchant,transfer_not_spbu,destination_not_merchant \
  --output /tmp/finance-extraction-paired.json
```

Remove `--case-ids` to use all labels. `--limit 3` selects the first three selected cases. Increase `--runs` to repeat the same cases, up to ten times, only when the shared budget permits. Budget exhaustion or provider unavailability aborts the run and writes the partial report with an `aborted` reason; an incomplete run is not a completed comparison. A full run can contain accuracy failures while exiting successfully: inspect its metrics, not only its process exit code. Exit code 2 identifies a setup error or aborted run.

The default baseline is Git revision `34b136acecbbcf6d163fab092f3960fd780d7c91`. `--baseline-ref` accepts a different **trusted local** revision. The evaluator reads its prompt constants without executing the prompt module, then compiles only the historical node constructor, OCR/extract/validate methods, and image normalizer from that revision. Historical staging, callback, graph, and persistence methods are excluded. Missing history is an explicit error; the evaluator does not silently substitute the current implementation.

Both variants intentionally share the **installed transport, Pydantic schemas, token accounting, and business validator**. This isolates historical versus current prompts, extraction/OCR boundary behavior, recovery, and image normalization. It is not a complete historical deployment benchmark, and it cannot measure changes in the transport's retry implementation or schema behavior. The report states this scope and includes the baseline commit and source hashes, current prompt hashes, dataset hash, selected IDs, model settings, and repeat number. Pair order alternates to reduce systematic warm-up bias. The provider remains nondeterministic; repeated runs and a larger held-out dataset are required before claiming general accuracy gains.

## Private receipt images

No private receipt images are committed with the dataset. To evaluate a real local image, create a private manifest outside the checkout. Label its printed amount, merchant, and date by inspecting the original image before running the model. Use a safe opaque ID and set the actual local file path:

```json
[
  {
    "id": "private_receipt_001",
    "kind": "receipt_image",
    "provenance": "real_local_image",
    "image_path": "/absolute/path/to/local-receipt.jpeg",
    "expected": {
      "intent": "expense",
      "amount": 33000,
      "merchants": ["EXPLICIT PRINTED MERCHANT"],
      "descriptions": [],
      "date": "2026-10-06"
    }
  }
]
```

The values above illustrate the manifest format; they are not labels for a real image. `descriptions: []` or `null` leaves description scoring unavailable for an unlabeled case. Do not use an empty list to hide a failure after evaluating a labeled case. Other expected fields are required. Optional `quantities` specifies the ordered decimal-string item quantities, for example `["1.5"]`.

```bash
envir/bin/python -m scripts.evaluate_extraction \
  --live --cases /tmp/private-receipt-cases.json \
  --variant both --runs 1 --output /tmp/private-receipt-results.json
```

Image files are checked against the configured file-size limit and then go through each variant's normalizer, OCR, extraction, and business validation. The evaluator does not download Telegram files, so Telegram delivery, photo selection, and actual network download behavior require their separate integration checks. Corrupted, oversized, or unsupported images fail at the input stage without financial writes.

## Reading the report

The report includes a boolean score for each labeled field and a per-metric denominator. `null` means unavailable or unscored, not zero.

| Metric | Definition and limitation |
| --- | --- |
| Amount exact match | Integer IDR equality for expected expense/income cases only; failed extraction counts as incorrect. |
| Merchant exact match | One of the predeclared alternatives, including explicit `null`; case, whitespace, and punctuation are normalized. |
| Description match | Normalized exact match against predeclared alternatives. It is a conservative wording proxy, **not a calibrated semantic judge**; a reasonable unlisted paraphrase can fail. |
| Date and quantities | Exact match for cases with those optional labels. |
| Hallucinated merchant | A non-null predicted merchant outside the supported alternatives. This detects unsupported names; a spelling variation may also be flagged. Missing a required merchant is an accuracy error, not a hallucination. |
| Unsupported transaction | A transaction object when the expected intent is nontransactional. This catches invented transactions on missing-data or negative cases; it is not a comprehensive hallucination audit of every receipt field. |
| Structured output validity | Valid schema responses divided by completed structured response attempts; transport/budget failures have unknown schema validity. A valid schema does not imply correct semantics. |
| Extraction schema validity | Whether a structured extraction result was observed, even if a later business rule rejected it. |
| Business validity | Whether the boundary returned successfully and transaction validation passed; intentional receipt rejection can be semantically correct while this metric is false. |
| Overall success | All labeled fields match, no unsupported transaction exists, and an expected transaction passed business validation. A structured `unknown` can correctly resolve a negative case. |
| Image receipt success | Overall success on expected transactions from actual image inputs. Separate from the transcription-only metric; unavailable when no images were evaluated. |
| First-pass success | Overall success with no semantic/OCR retry and no transport retry. Normal receipts have two different calls (OCR then extraction); these are not retries. |
| Retry frequency | Fraction of cases with extra calls within a role or an extra HTTP attempt. Semantic/OCR and transport retry rates are also separated. |
| p50/p95 latency | Wall time from local input preparation through validation, with linear percentile interpolation. Small samples are smoke checks, not stable percentile estimates. |
| Provider tokens and cost | Returned provider usage metadata. If any HTTP attempt lacks accounted usage, the affected aggregate is `null`, since that attempt may still have been billed. |
| Cost per successful transaction | Total measured evaluation workload cost divided by successful expected transactions, including failed and negative-case overhead. Unavailable if cost coverage is incomplete or there are no successful transactions. |
| CPU and memory | Per-case process CPU time and whole-process high-water RSS. CPU includes client/DB activity. RSS includes imported libraries and both variants; it is **not** isolated per-variant memory or server resource use. |

Output contains safe case IDs, provenance, booleans, counts, timings, hashes, stage names, and exception class names. It excludes raw input, OCR text, model text, merchant/description values, image paths, credentials, and exception bodies. Reports still reveal aggregate application usage and should be handled appropriately. No model is used as an evaluator or secondary OCR service.

## Acceptance and remaining coverage

Freeze labels before running either variant. Compare the same selected cases, source hashes, date context, repeat count, and model settings. Review individual failed field flags before summarizing accuracy. Claim only the results actually present in a completed report, including denominators, sample size, unavailable cost fields, and the shared-transport limitation. Deterministic tests and synthetic text cases do not prove live receipt accuracy.

Broader real-receipt coverage remains necessary: multiple stores/layouts, low resolution, rotation, faint printing, inclusive taxes, missing totals, and Telegram photo versus original document delivery. Separate unit tests validate corrupted input, refusal/incomplete output, API timeout/rate limiting, recovery limits, idempotency, and existing graph compatibility without spending API tokens. A small paired run is useful evidence for the reported cases; it is insufficient to establish population-level accuracy, reliability, latency, or cost improvements.

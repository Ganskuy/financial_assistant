# Monthly visualization extension

Status: a working deterministic PNG fallback is implemented. The requested GPT-6.1
Sol + React/Recharts path is **not complete**. No model, environment, dependency,
deployment, database schema, API, authorization, or infrastructure configuration
was changed. No live provider or Telegram calls were made during verification.

## Current behavior

```text
/visualize October [2026]
  -> existing authenticated BotService
  -> isolated graph using existing WorkflowState / chain / guarded patterns
  -> deterministic month parsing (Asia/Jakarta current year when omitted)
  -> user-scoped SQL daily/category aggregation, current + previous month
  -> deterministic presentation specification
  -> strict Pydantic validation and authoritative metric resolution
  -> DynamicChart (existing Pillow dependency: bar / line / pie)
  -> PNG bytes in the existing durable response JSON
  -> existing worker and TelegramClient, one multipart sendPhoto request
```

English/Indonesian month names are case insensitive; explicit `YYYY-MM` is also
accepted. Missing, malformed, and out-of-range dates produce usage guidance.
The existing report-year bounds apply. There are no ledger mutations or new
confirmation requirements. Opening money is excluded from monthly cashflow.

## Changed files

| File | Change |
| --- | --- |
| `app/telegram/router.py` | Add command to allowlist. |
| `app/services/bot.py` | Help entry, graph construction and isolated dispatch. |
| `app/agents/graphs/workflows.py` | Add a graph; existing graphs unchanged. |
| `app/agents/state.py` | Add visualization state fields. |
| `app/agents/nodes/visualization.py` | Parsing, aggregation, analysis boundary, rendering/fallback, safe timing logs. |
| `app/repositories/visualization.py` | One parameterized, user-scoped aggregate query. |
| `app/services/visualization.py` | Backend facts, compact payload, strict spec validation, metric resolution and grounded text. |
| `app/schemas/visualization.py` | Closed chart/insight decision schema. |
| `app/services/visualization_render.py` | In-memory Pillow fallback renderer. |
| `app/telegram/client.py` | Bounded PNG multipart upload through the same HTTP client. |
| `tests/unit/test_visualization.py` | Parsing, arithmetic, specification, graph failures, rendering and Telegram transport. |
| `tests/integration/test_visualization_workflow.py` | Real PostgreSQL + mocked external HTTP, user isolation and durable retries. |
| `docs/VISUALIZE.md` | Contract, verification and explicit implementation limits. |
| `README.md` | Command examples, architecture, feature behavior, limits, and runnable setup instructions. |
| `app/core/config.py` | Whitespace-only formatter correction during final checks; all settings unchanged. |

## Aggregation contract

`aggregate()` returns this structure; `compact_payload()` omits only the redundant
`weekly_expense` series and serializes compact JSON capped at the existing
6,000-character input envelope. **Production currently sends nothing to Sol**:
this is the implemented, tested input contract for an approved analyzer.

```text
period: {month: English month name, year: int, start_date: ISO date, end_date: inclusive ISO date}
overview: {
  total_income: integer IDR,
  total_expense: integer IDR,
  net_cashflow: integer IDR,
  savings_rate_pct: decimal string | null,
  income_transaction_count: int,
  expense_transaction_count: int
}
expense_by_category / income_by_category: [
  {category: string, amount: integer IDR, percentage: decimal string | null, transaction_count: int}
]
daily_expense: [{date: ISO date, amount: integer IDR}]
weekly_expense: [{week: int, amount: integer IDR}]
comparison: {
  previous_period_available: bool,
  previous_month_total_income: integer IDR | null,
  previous_month_total_expense: integer IDR | null,
  income_change_pct: decimal string | null,
  expense_change_pct: decimal string | null
}
```

One SQL statement sums and counts transactions by day/type/category for both
months, giving a consistent statement snapshot without N+1 queries. Python
integers combine database sums; percentages use `Decimal`, rounded half-up to
two decimal places. Zero denominator means `null`. A missing previous month is
explicitly distinguished from a previous month with income but no expenses.
Daily values cover the entire requested calendar month; weekly blocks are days
1–7, 8–14, etc., not ISO weeks. Largest individual expenses are not included;
category ranking and daily peaks are retained without raw transaction metadata.

## Validated presentation output

Example accepted by the actual `VisualizationSpec` schema:

```json
{
  "report": {
    "summary": "cashflow",
    "insights": ["top_expense", "savings_rate", "expense_comparison"]
  },
  "charts": [
    {"type": "pie", "metric": "expense_by_category"},
    {"type": "line", "metric": "daily_expense"},
    {"type": "bar", "metric": "income_vs_expense"}
  ]
}
```

- Allowed insights: `top_expense`, `top_income`, `expense_comparison`, `savings_rate`.
- Allowed metrics: `expense_by_category`, `income_by_category`, `daily_expense`,
  `weekly_expense`, `income_vs_expense`, `net_cashflow`, `monthly_comparison`.
- One to three charts and insights; chart metrics must be unique. Unknown keys,
  model-supplied data, narrative/code, unsupported types/metrics and incompatible
  type/metric combinations are rejected. Empty category/comparison selections
  are rejected when their data is unavailable.
- Pie requires a nonnegative distribution. Line requires a time series. Signed
  net cashflow uses a bar and zero baseline. Long category distributions combine
  the tail into an explicitly labeled sum for readability.
- Titles, summary and insight sentences are rendered from backend facts, using
  the same closed-decision approach as the existing grounded advisor. No model
  text, numbers, HTML, JSX or executable code reach the renderer.

An optional `Analyzer` callable accepts compact JSON and the schema class. Its
output is revalidated even if it returns a Pydantic object. It has an outer
deadline using the existing provider timeout. Model retries/token reservations
must remain inside the existing reviewed transport when that transport is wired;
the visualization workflow itself does not duplicate retries. The analyzer is
**not configured by BotService**; failure tests use mocks, not a real Sol adapter.

## Failure handling and privacy

- No activity: text response, no model or renderer call.
- Database failure: stage-safe log, then propagation to the existing durable
  worker. The queued update is retained for retry, not converted to a false
  empty report. The existing worker cannot send an error while its DB is down.
- Analyzer timeout/API/schema/metric failure: deterministic default charts.
- PNG rendering failure: verified textual summary through the existing sender.
- Telegram failure: the existing worker retains the same response and retries
  with its existing backoff/attempt limits. It never re-runs analysis on delivery
  retries. As with existing messages, ambiguous Telegram success followed by a
  lost HTTP response can cause duplicate delivery; exactly-once remote delivery
  is not guaranteed by this protocol.
- PNGs are capped at 2 MB, created in memory and stored as base64 in the existing
  response JSON. There are no runtime temporary files, browsers, subprocesses,
  background rendering tasks or new connections. Image/buffer contexts close on
  both success and exceptions. Photo+caption is one upload; no partial multipart
  message sequence was introduced. Caption is bounded to 500 codepoints; the
  complete summary remains in the image.
- Only stage/status/duration/error class is logged, with the existing request ID.
  Aggregates, images, provider output, account IDs, SQL and credentials are not logged.

The photo upload follows the [Telegram sendPhoto contract](https://core.telegram.org/bots/api#sendphoto).
The fallback uses [Pillow's bundled default font](https://pillow.readthedocs.io/en/stable/reference/ImageFont.html#PIL.ImageFont.load_default),
without depending on system fonts or adding dependencies.

## Exact blockers and smallest follow-up changes

1. **No GPT-6.1 Sol integration.** `app/llm/router.py` only reviews GPT-5.4 family
   profiles; `app/core/config.py` defaults the advisor to GPT-5.4. No verified Sol
   provider ID/token envelope exists in this codebase. Selecting a different
   existing model would silently violate the requested model choice. Enabling Sol
   needs an approved provider ID, reviewed token profile, an isolated feature role
   in `app/llm/router.py` / `app/llm/client.py`, feature model settings in
   `app/core/config.py`, and analyzer wiring in `app/services/bot.py` with a
   feature-specific prompt. Existing agent model selections must remain intact.
2. **No React/Recharts or browser export runtime.** There is no frontend package
   manifest, JavaScript runtime, screenshot service or browser in the Python
   deployment. Pillow is already installed for receipt images, so it supplies the
   safe PNG fallback. Actual Recharts export would need a small bundled React /
   Recharts report package plus an approved browser/export runtime and associated
   build dependencies/assets in `Dockerfile` (and its package manifest/lockfile).
   This cannot honestly be enabled without crossing the forbidden deployment /
   dependency boundary. No frontend shell or nonworking Recharts scaffold was added.

These changes require authorization under the request's explicit configuration
freeze. The implemented fallback does not satisfy the Sol/Recharts acceptance
criteria and must not be described as the complete target flow.

## Verification (2026-10-06)

Local dependencies: existing `envir`; PostgreSQL 14.18 in a disposable loopback
instance, fresh `finance_visualize_test` database migrated through revision 0003.
No user database was modified. External Telegram/model calls were mocked.

```bash
DATABASE_URL=postgresql+asyncpg://finance_test@127.0.0.1:55439/finance_visualize_test envir/bin/alembic upgrade head
TEST_DATABASE_URL=postgresql+asyncpg://finance_test@127.0.0.1:55439/finance_visualize_test envir/bin/pytest -q
envir/bin/ruff check app scripts tests alembic
envir/bin/ruff format --check app scripts tests alembic
envir/bin/pip check
DATABASE_URL=postgresql+asyncpg://finance_test@127.0.0.1:55439/finance_visualize_test envir/bin/alembic check
POSTGRES_PASSWORD=test-only-compose-check docker compose --env-file /dev/null config --no-env-resolution --quiet
```

- Full suite: 173 passed, including 50 new visualization tests; no integration skips.
- Existing agents/graphs initialize and command, receipt, advice, confirmation,
  opening balance, token budget, security and worker regression tests pass.
- Ruff lint, dependency consistency, migration drift and Compose validation pass.
- Repository-wide lint and formatting pass. Final pre-publication checks corrected
  one pre-existing line-wrap issue in `app/core/config.py`; its Python AST and all
  setting values are unchanged.
- No type checker is configured. Docker image build/run and PostgreSQL 16 are not
  verified: Docker daemon is unavailable. The old documented `.env.example` is
  absent; Compose was validated with an empty env file and a dummy password instead.
- PNG appearance was visually inspected using synthetic data. Live Telegram image
  quality/delivery and real Sol output remain unverified. No deployment performed.
- The disposable PostgreSQL instance was stopped and its temporary database/log
  files removed after verification. No test service remains running.

# Telegram Financial Assistant

A personal IDR finance assistant accessed through private Telegram chats. It captures expense/income text and receipt photos, stages every financial change for confirmation, and calculates balances and reports from an immutable PostgreSQL ledger. Budgets and savings goals also require confirmation. OpenRouter is the only model gateway.

`/visualize MONTH [YEAR]` produces a financial PNG report in Telegram for the selected month from confirmed ledger entries. Any calendar month is supported. It uses deterministic charts with the existing Pillow dependency and makes no AI calls. See [visualization behavior and limitations](docs/VISUALIZE.md).

## Architecture

```mermaid
flowchart TD
    TG[Telegram private chat] --> API[FastAPI webhook: secret, identity, rate limits]
    API --> Q[(PostgreSQL durable inbox)]
    Q --> W[In-process async workers]
    W --> R[Deterministic router]
    R --> C[Commands: zero model calls]
    R --> RX[Receipt LangGraph: OCR → extraction]
    R --> TX[Transaction LangGraph: extraction + intent]
    R --> AX[Advisor LangGraph]
    R --> CX[Confirmation / correction LangGraph]
    R --> VX[Visualization LangGraph: no model calls]
    VX --> VA[Scoped monthly SQL aggregation]
    VA --> DB
    VA --> VS[Validated chart specification]
    VS --> PNG[Pillow charts: PNG report]
    PNG --> Q
    W --> TG
    RX --> P[Validated pending operation]
    TX --> P
    P --> TG
    TG --> CX
    CX --> S[Finance / Pending services]
    AX --> T[Aggregated advisor context: injected user identity]
    T --> S
    C --> S
    S --> RP[Scoped repositories]
    RP --> DB[(PostgreSQL ledger / budgets / goals)]
    RX --> L[Shared LangChain model adapter]
    TX --> L
    AX --> L
    L --> B[Atomic global token reservations]
    B --> OR[OpenRouter]
    B --> DB
```

One API container and PostgreSQL. No Redis, queue server, vector database, RAG, or Kubernetes. Webhooks acknowledge only after committing an inbox entry. Workers use PostgreSQL row locks; responses are saved before Telegram delivery. Restarting the container retains accepted work, pending confirmations, ledger entries, and token reservations.

### Request flows and model calls

| Input | Flow | Expected AI calls |
|---|---|---:|
| `/balance`, `/history`, `/remove`, `/usage`, `/help` | SQL/static response | 0 |
| `/report [YYYY-MM]` | SQL category aggregation | 0 |
| `/visualize MONTH [YEAR]` | SQL aggregation → validated chart spec → PNG → Telegram | 0 |
| `/budget`, `/savings`, goal/budget commands | SQL or validated pending change | 0 |
| Confirm / Edit / Cancel | Ownership, version, expiry, transaction | 0 |
| Expense/income text | Combined classification + structured extraction → preview | 1 |
| Receipt image | Validate/download/decode → Nano OCR → Nano extraction → preview | 2 |
| Recognized report/advice question | Scoped summaries → advisor → checked fact references | 1, or 0 when empty |
| Other ambiguous natural language | Extraction may select report/advice → advisor | Up to 2 |

Each structured request permits at most two HTTP attempts for transient errors. Transaction extraction additionally permits one targeted recovery shared across OCR and extraction for malformed or inconsistent output; valid text still uses one request and valid receipts use two. Every attempt needs a full token reservation. No model fallback chain is enabled. All calls, including live evals and retries, share the same global budget.

Extraction now distinguishes explicit named merchants, generic businesses such as SPBU, products, destinations and unknown merchants. Receipt ingestion preserves transparent PNG text by compositing onto white. See [extraction changes and measured verification](docs/EXTRACTION_IMPROVEMENTS.md) and [reproducible evaluations](docs/EXTRACTION_EVALUATION.md).

### Backend aggregation and model efficiency

Financial aggregation happens before advisor model calls. `/report`, monthly `/balance`, `/budget`, and `/advice` share the existing monthly summary calculation: SQL groups confirmed transactions by type/category, and the backend combines those sums using integer IDR. The summary now requires one query. `/balance` without a month retains its separate opening-balance calculation.

Advisor context reuses that summary for budget spending and loads at most ten savings goals from the database. A nonempty month requires three data queries instead of six; an empty month requires one query and no model call. No cross-request cache is used, so newly confirmed entries are included on the next request.

The advisor receives compact JSON mapping fact IDs to authoritative amounts, for example:

```json
{"income":10000000,"expenses":100000,"balance":9900000,"category.food":100000}
```

Budget and savings facts are included when present. Labels are kept on the backend for rendering, and transaction rows, descriptions, merchants, and savings-goal names are excluded from advisor input. A regression test with 100 expenses and 10 income entries verifies that input size follows the number of aggregate facts rather than the number of transactions. Existing schema validation, grounded replies, deadlines, retry limits, and global token reservations still apply. Extraction/OCR necessarily reads the newly submitted text or receipt; it does not load historical ledger entries.

`/visualize` keeps its separate daily/category aggregation because charts need time-series data and a previous-month comparison. Other commands avoid retrieving that extra detail. These changes reduce query count and model-input bytes; live provider latency, billed tokens, and monetary savings have not been benchmarked.

### Modules and database

```text
app/
  api/                  Webhook and health routes
  agents/
    graphs/             Receipt, transaction, advisor, confirmation, visualization graphs
    nodes/              Model, validation, staging, gathering and rendering nodes
    prompts/            Versioned trust-boundary prompts
    state.py            Typed graph state
    tools.py            Seven scoped LangChain read tools
  core/                 Settings, safe errors, DB sessions, structured logs
  llm/                  OpenRouter transport, role profiles, token accounting
  models/               SQLAlchemy table definitions
  repositories/         Scoped financial queries and durable inbox
  schemas/              Strict financial schemas and Telegram transport schemas
  services/             Business rules, pending operations, advice, chart rendering, worker
  telegram/             Bot API transport, safe image decoding, routing
alembic/versions/        Versioned schema and edit-recovery migrations
scripts/                Webhook setup, model catalog checks, opt-in live eval
tests/                  unit/, integration/, evals/
```

The normalized tables are `opening_balances`, `users`, `transactions`, `transaction_items`, `pending_transactions`, `callback_actions`, `budgets`, `savings_goals`, `savings_contributions`, `daily_llm_usage`, `llm_reservations`, `processed_telegram_updates`, and `rate_buckets`. UUIDs identify internal financial entities; Telegram identities map to user UUIDs. Indexes cover user/date, user/type/date, user/category/date, pending ownership, queue state, and expiry. Unique constraints enforce update/message deduplication and one committed ledger entry per pending operation. DB triggers prohibit ledger UPDATE/DELETE.

`/balance` shows confirmed opening money plus all subsequent recorded income minus expenses, carried across months. Without an opening entry it explicitly reports only recorded net change. `/balance YYYY-MM` and `/report` still show monthly income minus expenses; opening money never inflates income. This tracks your records, not a live bank account. IDR uses integers; fractional item quantities use Decimal. Savings contributions are earmarks and do not change the income/expense ledger. An existing goal name or category/month budget is replaced only after a new confirmation.

LangGraph persists important workflow checkpoints explicitly in application tables. It does not store financial truth in graph memory. An edit resumes the pending record, revalidates allowlisted fields, and rotates the preview version. All previous buttons become stale. A lost correction response can be recovered by its recorded update ID.

## Local setup (Python 3.11)

Prerequisites: Python 3.11, Docker with Compose (or PostgreSQL 14+ installed locally), a Telegram bot, and an OpenRouter key. PostgreSQL 16 is the development/CI container version.

```bash
cd /Users/haifanghani/Documents/financial_assistant
python3.11 -m venv envir
source envir/bin/activate
pip install -r requirements.txt
touch .env
python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Edit `.env`. Generate `APP_SECRET_KEY` and `TELEGRAM_WEBHOOK_SECRET` independently. Set the bot token, allowed numeric Telegram user IDs, OpenRouter key and PostgreSQL password. Use a URL-safe random PostgreSQL password (the generator above is suitable) or correctly URL-encode the password in `DATABASE_URL`. No real credentials are provided or committed.

There is no committed `.env.example`. Add these required values to your local `.env`; replace every angle-bracket placeholder before running:

```dotenv
APP_SECRET_KEY=<generated-secret>
TELEGRAM_WEBHOOK_SECRET=<different-generated-secret>
TELEGRAM_BOT_TOKEN=<BotFather-token>
TELEGRAM_ALLOWED_USER_IDS=<your-numeric-Telegram-user-id>
OPENROUTER_API_KEY=<your-OpenRouter-key>
POSTGRES_PASSWORD=<URL-safe-database-password>
DATABASE_URL=postgresql+asyncpg://finance:<same-URL-safe-database-password>@127.0.0.1:5433/finance
```

Start only the database, migrate, then run the API:

```bash
docker compose up -d postgres
alembic upgrade head
python -m scripts.check_models
uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log
```

Compose exposes PostgreSQL on host port **5433**; the database container uses port 5432 internally. To use an existing PostgreSQL server instead of Docker, create a dedicated role/database and set `DATABASE_URL` to that server. Runtime settings use python-dotenv without overriding injected environment variables; container secret injection works normally. `APP_SECRET_KEY` is used for keyed identity hashes in logs, not as a replacement for the webhook secret.

Check the API:

```bash
curl --fail http://127.0.0.1:8000/health
curl --fail http://127.0.0.1:8000/ready
```

`/health` checks process liveness. `/ready` checks PostgreSQL connectivity, schema/migration revision, and worker tasks. Neither calls an LLM. Financial data and application metrics are not exposed over HTTP; Telegram remains the only financial UI.

## Telegram setup

1. Create a bot using Telegram's official `@BotFather` and put its token in `.env`.
2. Set `TELEGRAM_ALLOWED_USER_IDS` to your numeric user ID. Obtain it from a trusted Telegram client/export or bot identity workflow. The application rejects every ID not explicitly listed and rejects group chats.
3. Deploy behind HTTPS or temporarily expose port 8000 through a trusted HTTPS tunnel for local testing. The public URL must end in `/telegram/webhook`.
4. Register the webhook using the script. It reads secrets from the environment and does not print them or put bot tokens in shell arguments.

```bash
python -m scripts.set_webhook https://YOUR_PUBLIC_HOST/telegram/webhook
```

The script sets `secret_token`, accepts only `message` and `callback_query` updates, and retains queued Telegram updates. Send `/help` in your bot's private chat. Test income, expense, receipt, Edit, Cancel, and repeat Confirm in a staging bot before production. Without bot credentials and a reachable HTTPS endpoint, live Telegram delivery cannot be verified.

### Initialize your current money

Before recording income or expenses, send either:

```text
Saat ini aku memiliki uang sebanyak 813.794 rupiah
/opening 813794
```

Both produce an opening-balance preview without an AI call. Tap **Confirm** to initialize it, **Edit** to correct `amount`, or **Cancel**. An opening amount can be zero and is stored separately from income. It can be set only once, before the first confirmed transaction. The application refuses to replace it, initialize after transactions exist, or add transactions dated before the opening date. This avoids double-counting existing money. Only record income/expenses occurring after that starting baseline. `/balance` then shows the current tracked balance across months; `/report` continues to show monthly financial activity.

### Commands

`/remove expenses` or `/remove income` shows your latest 10 active entries of that
specific type, ordered by when they were saved. The aliases `\remove expenses`
and `\remove income` also work. Tap **Remove #N**, review the selected entry, then
**Confirm** or **Cancel**. Selection alone changes no financial totals. Buttons
stay tied to their original entries even if new transactions arrive; selections
expire after the normal pending TTL and can be reopened with `/pending`.

Removal excludes the entry from history, balances, reports, budget spending,
charts, duplicate warnings, and advisor facts. It does not delete budgets or
change savings earmarks or opening money. The original transaction and receipt
items remain in the immutable ledger, with a separate immutable removal audit
record; this is a logical deletion, not permanent erasure. Repeated confirmation
cannot remove twice, and another user's buttons cannot authorize a removal.
Removal uses no model calls. Removing all entries does not permit setting a new
opening balance after transactions have existed.

For an existing installation, stop the API/workers, activate `envir`, run
`alembic upgrade head` (migration `0004`), then restart the API/workers and check
`/ready`. Do not run the updated code against migration `0003`. Downgrade refuses
when removal operations or audit records exist, to prevent silently restoring
removed entries.

`/history` lists confirmed transactions by when they were saved, newest first,
including their merchant and original transaction date. Each page contains up to
10 entries; use `/history 2` (or the next-page command in the response) for older
entries. Newly confirmed receipts appear first even when the printed purchase
date is earlier. Pending previews appear under `/pending`, not `/history`.

```text
/opening 813794
/balance
/report 2026-10
/visualize MONTH [YEAR]
/history
/history 2
/remove expenses
/remove income
/usage
/budget
/budget 2026-10 | food | 1 juta
/goal Emergency fund | 10 juta | 2027-12-31
/save Emergency fund | 500 ribu
/savings
/advice
/pending
/help
```

Natural-language examples: `Tadi beli kopi 25 ribu di Point Coffee`, `Aku dapat gaji 7 juta`, `Bagaimana pengeluaran saya bulan ini?`, `Buat laporan keuangan Oktober.` Named months without a year use the current Jakarta year. Unsupported/unclear messages do not write records.

After selecting **Edit**, send corrections as one or more lines:

```text
amount=30 ribu
category=food
transaction_date=2026-10-05
```

Only allowlisted fields can change. Expense/income type and currency cannot be altered through arbitrary field injection. Receipt amounts cannot be changed independently of their item/tax arithmetic: cancel and submit corrected text instead. Use `/pending` to reopen active previews. Pending operations expire after 30 minutes by default; editing does not extend the original expiry. A confirmed entry is immutable.

### Monthly visual reports

```text
/visualize MONTH [YEAR]
/visualize YYYY-MM
```

Replace `MONTH` with any English or Indonesian month name; names are case insensitive. `MONTH` is required and `[YEAR]` is optional—omit the brackets when entering a year. Alternatively, use `YYYY-MM` for a numeric year and month. Each request covers one calendar month. Without a year, the command uses the current year in **Asia/Jakarta**. Missing/invalid arguments return usage guidance; months without transactions return a short no-data message.

Reports show income, expenses, net cashflow, leading categories, and a previous-month spending comparison when available. Months with expenses include an income/expense bar chart, expense-category pie chart, and daily spending line chart. Income-only months show income/expense and income-category bar charts. All figures come from confirmed transactions belonging to the requesting user; opening money is excluded from monthly cashflow.

The graph aggregates both months in one database query and uses integer IDR and Decimal percentages. Chart specifications accept only supported types and metrics; they cannot supply replacement financial values or executable code. PNG generation stays in memory. The existing durable worker stores the response before delivery and reuses it on Telegram retries. If rendering fails, the bot returns verified text instead. As with existing messages, an ambiguous Telegram timeout can still cause duplicate delivery.

This feature needs no new environment variables, dependencies, migrations, frontend, or browser runtime. **Reports use deterministic Pillow rendering.** An optional analyzer boundary is schema-validated and tested with mocks; it is not enabled in the running bot. See [the full contract, failure behavior, tests, and limitations](docs/VISUALIZE.md).

## OpenRouter and the global 100,000-token limit

Defaults use `openai/gpt-5.4-nano` for image transcription and structured extraction, and `openai/gpt-5.4` with low reasoning effort for the advisor. Model slugs and API shapes were checked against official documentation. Availability changes: run `scripts.check_models` before deployment. The requested Nano model is retained even though its upstream documentation currently flags deprecation; OpenRouter catalog/account availability must be monitored.

The shared adapter uses async HTTPX, LangChain message/Runnable abstractions, strict JSON Schema output and Pydantic validation. It requires provider parameter support and disables provider fallback. It never parses financial values from free prose. Schema failures are charged and not retried. 429, network timeout and 5xx failures permit at most one retry with exponential backoff/jitter and a fresh reservation. Permanent errors are not retried. The total call deadline is bounded.

Before every call, PostgreSQL atomically checks and reserves:

```text
used today + reserved today + unresolved prior calls + worst-case new allowance <= 100,000
```

A transaction-level advisory lock serializes reservation/reconciliation across workers and replicas. Input allowances include all text, JSON schemas, framing and bounded image tokens; output allowances include reasoning. Reported usage replaces a reservation and releases unused capacity. No credentials, calls, prompts or generated amounts can raise the configured cap above 100,000.

Reset is midnight **Asia/Jakarta**. Calls crossing midnight consume conservative capacity on both days. Unknown usage is never refunded just because of timeout, failure, restart, or midnight. `/usage` shows used, reserved/uncertain and remaining available tokens. A call can be refused while confirmed usage is below 100,000 because its entire worst-case allowance must fit.

**Trust boundary:** the concurrency/admission limit is enforced locally; actual remote consumption still depends on the provider honoring its token cap and the reviewed token envelope. Arbitrary configurable model slugs are rejected unless they have a reviewed profile. A provider usage violation trips a persistent breaker rather than silently allowing more calls. Unresolved reservations can reduce subsequent days' capacity until an operator obtains authoritative usage evidence. See [the full design and official references](docs/DECISIONS.md).

## Security model

- Constant-time webhook-secret comparison, JSON/body-size limits, supported updates, fresh messages, private chats and explicit user allowlisting.
- PostgreSQL-backed rate limiting and durable update/message deduplication. Independent callbacks are authorized against pending ownership, expiry and version.
- Financial writes are reachable only through a validated pending operation and an explicit owner callback; even high-confidence extraction never commits. Budget/goal/contribution writes use the same policy.
- SQLAlchemy bound parameters, server-side field/category allowlists, scoped repository instances. No model-generated SQL, shell, file, HTTP or secret tools.
- OCR, chat and stored text are untrusted. They cannot set identity, select database objects, bypass confirmation or become executable instructions. Advisor tools omit descriptions and goal names from model context.
- Advisor output is a closed set of action identifiers and validated fact references. Authoritative numbers and final prose come from the backend. Invalid references or contradictory advice fall back to deterministic reports.
- Receipt images are streamed from fixed Telegram URLs with no redirects, checked for byte size, decoded as JPEG/PNG, bounded for decompression, stripped of EXIF, and resized to at most 1024 pixels per side. No receipt image is stored permanently.
- Structured logs contain correlation IDs, keyed user hashes, role/model, latency, token usage, cost when provided, and safe error types. No API keys, bot URLs, raw images, prompts or histories. LangSmith tracing is explicitly disabled at graph/model invocation.
- Use a dedicated OpenRouter key and restrict provider retention/account logging as appropriate. Requests set provider `data_collection=deny`; confirm your OpenRouter/provider retention settings separately.
- `.env` is ignored and excluded from Docker. Runtime container is non-root, read-only in Compose, drops Linux capabilities, and uses no-new-privileges.

## Tests and verification

```bash
source envir/bin/activate
pip check
ruff check app scripts tests alembic
ruff format --check app scripts tests alembic
python -m compileall -q app scripts alembic
pytest -q
```

Without `TEST_DATABASE_URL`, PostgreSQL integration tests are explicitly skipped. Unit and policy/eval tests use mocked OpenRouter/Telegram. Integration tests **truncate all application tables** in the supplied test database and refuse names not ending in `_test`. Never point them at real financial data. Do not run these fixtures concurrently with pytest-xdist against one database.

Create and migrate a disposable database:

```bash
docker compose exec postgres createdb -U finance finance_test
export TEST_DATABASE_URL='postgresql+asyncpg://finance:YOUR_URL_SAFE_PASSWORD@127.0.0.1:5433/finance_test'
DATABASE_URL="$TEST_DATABASE_URL" alembic upgrade head
DATABASE_URL="$TEST_DATABASE_URL" alembic check
pytest -q
```

CI provisions PostgreSQL 16, applies migrations, checks model/schema drift, runs lint/format checks, executes the full suite, and builds the Docker image. Real provider calls are never part of CI.

**Removal feature verification (2026-10-10):** 343 tests passed with no skips against a disposable PostgreSQL database, including user isolation, concurrent confirmation, stale selections, aggregate consistency, and the mocked webhook/worker flow. Lint, formatting, dependency checks, migration upgrade/downgrade/upgrade, schema-drift checks, destructive-downgrade refusal, and the Docker image build passed. Live Telegram delivery was not exercised.

Earlier local verification on 2026-10-06: **173 tests passed**, including 50 visualization tests and the full existing regression suite, using an isolated PostgreSQL 14.18 database with mocked external APIs. Lint, formatting, dependency consistency, migration-drift checks, and Compose validation passed. Live Telegram delivery and a local Docker image build were not verified; Docker was not running. See [visualization verification details](docs/VISUALIZE.md).

Optional, billed extraction evaluation against the golden fixtures:

```bash
python -m scripts.live_eval --live
```

This uses the production-style global token budget and writes no financial records. It is separate from offline regression tests. See [acceptance criteria and assumptions](docs/DECISIONS.md) and [local verification results](docs/VERIFICATION.md).

## Docker and production deployment

Build and run the local stack after editing `.env`:

```bash
docker compose build api
docker compose up -d postgres
docker compose run --rm api alembic upgrade head
docker compose up -d api
```

Both exposed ports bind only to loopback. The PostgreSQL volume persists through container restarts; do not use `docker compose down -v` with data you need. A multi-stage Python 3.11 Dockerfile installs the pinned dependency set once and runs as UID 10001.

For a single API container plus managed PostgreSQL:

```bash
docker build -t financial-assistant:release .
# Configure .env.production locally or inject equivalent platform secrets.
docker run --rm --env-file .env.production financial-assistant:release alembic upgrade head
docker run -d --name financial-assistant --restart unless-stopped \
  --env-file .env.production --read-only --tmpfs /tmp:rw,noexec,nosuid,size=16m \
  --cap-drop ALL --security-opt no-new-privileges:true \
  -p 127.0.0.1:8000:8000 financial-assistant:release
```

Production procedure:

1. Provision a managed PostgreSQL database with TLS, backups/PITR, and private or restricted networking. Set `DATABASE_URL` to its `postgresql+asyncpg://` URL using `?ssl=require` (or the provider's CA-verified SSL setup). Use separate migration and runtime roles. Runtime needs table SELECT/INSERT and workflow/budget UPDATE plus schema usage; it does not need CREATE, TRUNCATE, or ledger UPDATE/DELETE. Test permissions in staging.
2. Inject the settings listed under Local setup with `APP_ENV=production`, real secrets and the allowed Telegram IDs. Keep one dedicated OpenRouter key. Pin base-image digests and the release image digest in the deployment manifest after building/scanning them.
3. Back up PostgreSQL, run `alembic upgrade head` as a separate release job, then start the image. Runtime startup never silently creates or migrates tables.
4. Terminate HTTPS in the hosting platform or a reverse proxy. Set request-size/time limits, restrict backend port 8000, and forward the Telegram secret header unchanged. Do not log full webhook bodies or secret headers. No browser UI is required.
5. Verify `/ready`, run catalog checks and staging acceptance, then register the webhook. Keep database connections within the managed-plan allowance: each API process uses an 8-connection pool with up to 4 overflow connections.
6. For a canary/blue-green rollout, start the new image alongside the old one against a backward-compatible schema, verify readiness, then move webhook ingress. PostgreSQL locks and unique constraints protect concurrent workers. Drain/stop the old process; uncommitted inbox claims roll back. For incompatible migrations, pause ingress and workers during the migration instead.
7. Roll back by deploying the previous compatible image digest. Use forward-compatible migrations; do not blindly downgrade a live financial database. Restore backups only under a planned recovery procedure, and reconcile Telegram update/token state before resuming.

No GPU or model-serving infrastructure is needed. Start with one API process and two workers. Add replicas only if measured queue latency justifies it; the token cap remains global. Managed PostgreSQL and model calls are the main ongoing costs. Autoscaling should be bounded by DB connection capacity and the deliberately small daily AI allowance.

## Configuration

| Variable | Purpose / default |
|---|---|
| `APP_ENV` | development, test, production |
| `APP_SECRET_KEY` | Required independent 32+ ASCII URL-safe secret for log identity hashing |
| `APP_TIMEZONE` | Fixed `Asia/Jakarta` |
| `TELEGRAM_BOT_TOKEN` | Required BotFather token |
| `TELEGRAM_WEBHOOK_SECRET` | Required independent 32+ URL-safe secret |
| `TELEGRAM_ALLOWED_USER_IDS` | Required comma-separated numeric IDs; no open enrollment |
| `DATABASE_URL` | Required asyncpg PostgreSQL URL |
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | Compose database initialization only |
| `OPENROUTER_API_KEY` | Required dedicated gateway key |
| `OPENROUTER_BASE_URL` | Fixed `https://openrouter.ai/api/v1` to prevent credential redirection |
| `OPENROUTER_VISION_MODEL` | `openai/gpt-5.4-nano` |
| `OPENROUTER_EXTRACTION_MODEL` | `openai/gpt-5.4-nano` |
| `OPENROUTER_ADVISOR_MODEL` | `openai/gpt-5.4` |
| `OPENROUTER_TIMEOUT_SECONDS` | 45; range 1–90 |
| `OPENROUTER_MAX_ATTEMPTS` | 2; maximum 2 |
| `DAILY_LLM_TOKEN_LIMIT` | 100,000; may be lowered, never raised |
| `VISION_MAX_OUTPUT_TOKENS` | 1,800 |
| `EXTRACTION_MAX_OUTPUT_TOKENS` | 1,600 |
| `ADVISOR_MAX_OUTPUT_TOKENS` | 1,200 |
| `MAX_RECEIPT_FILE_SIZE_MB` | 5; maximum 10 |
| `PENDING_TRANSACTION_TTL_MINUTES` | 30 |
| `RATE_LIMIT_PER_MINUTE` | 20 per Telegram user |
| `WORKER_COUNT` | 2; range 1–4 |
| `MAX_UPDATE_AGE_HOURS` | 24; callbacks use pending expiry |

## Failure behavior and operations

| Condition | Behavior |
|---|---|
| Invalid webhook secret/user/group | HTTP 403, no work accepted |
| Malformed / oversized update | HTTP 400 / 413 |
| DB unavailable during ingestion | HTTP 503 so Telegram can retry |
| Duplicate update or message | Acknowledge without another job |
| Rate limited | HTTP 429 + Retry-After; no new accepted job |
| Invalid/oversized/unreadable image | Safe Telegram error; no financial write |
| Malformed extraction/schema | No pending financial write; usage still accounted |
| Low confidence or inconsistent arithmetic | Warning in preview; explicit confirmation still required |
| Wrong-owner/stale/expired callback | Safe unavailable response; no change |
| Daily allowance insufficient | No OpenRouter call; SQL commands continue |
| AI failure during advice | Deterministic verified report remains available |
| Telegram response delivery failure | Persist response; capped exponential retry, then failed job |
| Crash before/after confirmation | Pending data/unique ledger entry is recovered; no duplicate financial write |
| Provider usage exceeds reservation | Persistent AI breaker, actual usage retained for audit |

Collect JSON stdout logs. Alert on `worker_failure`, `workflow_failure`, `telegram_delivery_failure`, repeated `llm_failure`, failed readiness, increasing queued-update age, and sustained uncertain reservations. Derive request/model latency, token counts and reported cost from logs; DB query timing is available at DEBUG without logging SQL or parameters. Correlation IDs are `tg-<update_id>`. Do not enable SQL echo or raw HTTP debug logging.

An operator can inspect queue status and reservations through an authenticated DBA connection; there is no public administrative endpoint. Keep idempotency tombstones indefinitely unless a deliberate replay-retention policy replaces them. Completed inbox payloads and responses are cleared; failed responses should be retained briefly for recovery then purged using `python -m scripts.maintenance prune`. Run `python -m scripts.maintenance status` for safe queue/token counts. Financial records/pending audit data are sensitive: use encrypted disks/backups, least-privilege access, and an explicit retention policy appropriate to your use.

Limitations requiring live deployment validation: real Telegram delivery, OpenRouter account access and OCR/extraction quality need your credentials; mock tests do not establish those facts. Receipt downscaling may make very small print unreadable. Advisor prose is intentionally constrained to preserve exact grounding. See the verification record for what was actually run.

# Verification record — 2026-10-05 (Asia/Jakarta)

## Aggregation efficiency — 2026-10-07

- Full local regression suite: **178 passed**, with an isolated PostgreSQL 14.18 database migrated through revision 0003 and mocked external APIs.
- Monthly summaries use one SQL aggregate query. Advisor context uses three data queries for an active month and reuses the same expense totals for budgets; empty months use one query and no model call.
- A 110-transaction regression fixture (100 expenses, 10 income entries) verifies exact aggregated amounts and a model payload that contains only four facts for its two categories, without raw transaction metadata.
- Tests cover unchanged command summaries, user isolation, visualization/summary parity, ten-goal advisor limit while `/savings` retains all goals, compact fact-ID rendering, large-integer precision, and input-envelope rejection.
- No live provider cost/latency benchmark was performed. Existing model configuration, token-budget policy, database schema, dependencies, and deployment settings are unchanged.

## Executed successfully

- Created the requested `envir` using Python 3.11.14 and installed `requirements.txt` from PyPI. Resolved dependencies were frozen; `pip check` reported no broken requirements.
- Ruff lint and formatting checks passed for application, scripts, migrations, and tests. Python compileall passed.
- Initialized a separate disposable PostgreSQL 14.18 instance on loopback port 55439. The test database contained only generated fixtures, not user financial data.
- Alembic upgraded from an empty database through revisions `0001` and `0002`. A full downgrade to base followed by upgrade to head succeeded. `alembic check` reported no new upgrade operations.
- Full suite: **95 passed**, including **17 PostgreSQL integration tests** and **78 unit/offline policy regression cases**. No integration skips in this run.
- Tests include concurrent token reservations, idempotent reconciliation, Jakarta midnight rollover and unresolved carryover, concurrent confirmations, immutable ledger enforcement, user isolation, edit recovery, webhook security/idempotency/rate limits, delivery retries, receipt OCR/extraction orchestration and receipt item commits.
- FastAPI `/health`, `/ready`, webhook ingestion, durable worker processing, and confirmation were exercised through ASGI with real PostgreSQL and mocked Telegram/OpenRouter network responses.
- Docker Compose configuration validated with dummy configuration values using `docker compose --env-file .env.example config --no-env-resolution --quiet`. No real secrets were supplied.

## External verification limits

- No Telegram bot token, OpenRouter key or public HTTPS URL was provided. Real Telegram delivery, real provider generation, model account access, real-receipt OCR accuracy and live extraction/advice quality were not tested. No paid model calls were made.
- Offline eval fixtures test schemas and application policy; they do not establish model accuracy. An opt-in live evaluator is included and uses the same shared budget.
- Docker Desktop was initially stopped and was launched for verification. Image build attempts reached Docker Hub metadata retrieval but the registry request timed out. A successful container image build/run is therefore **not claimed**. The included CI workflow builds the image when registry access is available.
- PostgreSQL 16 is configured in Compose/CI; the local integration run used installed PostgreSQL 14.18. The GitHub Actions workflow itself was not run or published.
- Actual external model consumption depends on the reviewed token envelope and provider honoring its cap/usage contract. The application concurrency invariant and fail-closed behavior were tested; no backend can retroactively prevent a provider from violating its own API contract.

## Commands used for the final full suite

```bash
DATABASE_URL=postgresql+asyncpg://finance_test@127.0.0.1:55439/finance_test envir/bin/alembic downgrade base
DATABASE_URL=postgresql+asyncpg://finance_test@127.0.0.1:55439/finance_test envir/bin/alembic upgrade head
DATABASE_URL=postgresql+asyncpg://finance_test@127.0.0.1:55439/finance_test envir/bin/alembic check
TEST_DATABASE_URL=postgresql+asyncpg://finance_test@127.0.0.1:55439/finance_test envir/bin/pytest -q
```

These URLs refer only to the temporary verification database. Use your own migrated test database for future runs. The integration fixture refuses to truncate a database whose name does not end in `_test`.

## Opening-balance verification — 2026-10-06

- A separate `finance_opening_test` database on the local PostgreSQL 16 container was migrated through revision 0003. No real financial database was used for test truncation.
- `alembic check` reported no schema drift. The complete suite passed: **115 tests**, including the two exact screenshot inputs, zero-AI routing, explicit confirmation, concurrent opening attempts, duplicate callbacks, edits/cancellation, zero opening amounts, cross-user isolation, monthly carry-forward, and commit-time checks against existing/backdated transactions.
- The normal application database was upgraded to revision 0003 without inserting any opening balance or changing existing financial records. The user must submit and confirm the actual amount in Telegram.

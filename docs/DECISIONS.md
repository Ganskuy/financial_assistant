# Engineering decisions and acceptance criteria

## Baseline and quality gates

Use deterministic commands and parameterized SQL first. Use one structured extraction call for ordinary transaction text; use OCR plus extraction for images. Use no general-purpose autonomous agent or chat-history memory. Human confirmation is mandatory even for high confidence. The minimum release gates are:

| Property | Required result | Verification |
|---|---|---|
| Financial writes before confirmation | Zero | PostgreSQL integration tests |
| Duplicate ledger writes under concurrent confirmation | Zero | Concurrent callbacks + unique constraints |
| Cross-user reads or mutations | Zero | Scoped repositories, callback/edit/tool tests |
| Reservation admission beyond 20,000 tokens | Zero | Twenty concurrent reservation attempts |
| Unaccounted retries / uncertain calls | Zero | Mock transport + persisted reservations |
| Authoritative numbers invented by advisor | Zero | Closed action schema, reference checks, deterministic rendering |
| Extraction exact amount/intent on golden fixtures | 100% on small fixture set before release | Opt-in live evaluation; not claimed from mocks |
| Financial arithmetic using binary floats | Zero | Strict integer IDR and Decimal quantities |
| Receipt OCR fidelity on real receipts | Human acceptance on representative clear and difficult photos | Requires staging bot/account and real examples |
| Latency / spend | Observe p50/p95 latency and provider-reported cost by role | Structured log aggregation in deployment |

No actual extraction-accuracy benchmark, real-world OCR score, or provider latency is claimed by the offline tests. Mocked provider tests exercise application policy and response handling. `scripts/live_eval.py --live` makes real billed calls through the same budget. It does not write financial data. Add real receipt fixtures only with the owner's consent and redaction of unnecessary personal information.

## Architecture and tradeoffs

- PostgreSQL serves the ledger, durable inbox, pending operations, token reservations, and rate limiter. No separate broker is necessary for a personal assistant.
- A worker keeps a selected inbox row locked during processing. Business operations and token reservations use independent short transactions. `SKIP LOCKED` and per-user advisory locks support multiple workers/replicas. Limit connections accordingly. Keep the DB's idle-in-transaction timeout above 450 seconds for the inbox connection.
- At most one queued/responding update per user progresses at a time. It prevents an edit/callback from overtaking an earlier accepted update. Arrival order across Telegram connections is not a globally ordered protocol; server-side pending versions remain the authorization barrier.
- Model/receipt processing before a durable checkpoint can be repeated after an abrupt crash. Each attempt is separately reserved. Once pending state or a response is durable, recovery uses it. Corrections store their update ID to avoid re-extraction after a crash.
- Telegram sends have no client idempotency key. A crash after Telegram accepts a message but before the DB acknowledges delivery can duplicate a notification. Ledger writes remain at-most-once. Do not claim exactly-once messaging.
- Savings contributions are immutable earmarks, not extra income/expenses, and do not reduce the ledger-derived cash balance. They do not verify that money moved to a bank account. Creating a goal with the same name updates its target/deadline after confirmation.
- Expenses and income are immutable; correcting a committed ledger entry is deliberately outside this release's Telegram command surface. Edit applies only to pending entries. A subsequent correction feature must use confirmed reversal entries rather than direct UPDATE/DELETE.
- Only IDR, private Telegram chats, and explicitly allowlisted Telegram IDs are supported. No currency conversion, shared chat accounting, or bank integration.
- The AI advisor selects useful statements and fact references from a closed schema. The backend writes the final prose and numbers. This limits expressiveness but makes exact numerical grounding enforceable. It gives general budgeting guidance, not investment/tax/credit recommendations.
- Tool calls are deterministically prefetched through LangChain StructuredTool schemas. The model has no autonomous tool loop. This avoids extra round trips and prevents arbitrary tool execution. Seven narrow read tools are available to the orchestrator; identity is captured in the FinanceService instance.
- LangGraph owns sequencing, branching, error transitions, edit/callback resumption. App tables persist the required checkpoints. A separate LangGraph checkpointer would duplicate state for this workflow and is intentionally omitted.
- A separate static application circuit breaker blocks all future AI calls if observed usage exceeds a reservation. Unknown calls keep durable reservations and can exhaust AI capacity; that is safer than guessing that they cost zero. Deterministic features remain available.

## Token-bound trust contract

The hard cap is an application admission invariant, not an OpenRouter account-wide quota. It covers this app's model calls; a shared API key used by other applications is outside its control. Use a dedicated key.

Each call reserves the entire serialized text/schema UTF-8 byte count (a conservative envelope for the reviewed byte-based tokenizer profiles), 4,096 framing/schema-translation overhead tokens, the configured maximum output, and 4,096 image tokens for a single canonical JPEG of at most 1,024 × 1,024 pixels. Images use `detail=high`. Current documented GPT-5.4 Nano image patch behavior fits comfortably inside this envelope; the oversized image allowance also covers the older documented multiplier. Do not replace this with `characters / 4` or count raw base64 as textual model input.

Provider usage must include prompt plus completion/reasoning tokens and honor `max_tokens`. There are no tools/plugins, response healing, hidden fallback chains, or streaming calls. Provider routing requires parameter support and disables provider fallback. The backend cannot mathematically guarantee the behavior of a remote provider that violates its API contract or silently changes tokenization/framing. This is an explicit trust boundary, not a guarantee supplied by a prompt. A usage-bound violation records actual usage, trips a persistent breaker, and needs operator investigation. Unknown models are rejected until their tokenizer/envelope/output-cap profile is reviewed and tested in `app/llm/router.py`.

Admission and reconciliation share a PostgreSQL transaction advisory lock. The condition is:

`today.used + today.reserved + unresolved_previous_days + new_reservation <= configured_limit <= 20000`

Settled calls refund only the unused reservation. Missing/invalid usage, timeouts, cancellations, 429s, 5xxs and permanent HTTP errors keep the allowance because the backend cannot prove the provider did not consume tokens. Retries obtain new reservations and can be denied. Dates use Asia/Jakarta. An in-flight call crosses midnight as reserved capacity; on completion its actual usage is conservatively charged to both start and completion dates. Settled activity resets at local midnight. Unresolved reservations never disappear merely because time passed or the API restarted.

Unknown calls can reduce capacity on later days. Do not clear them on a timer. Reconcile only after obtaining authoritative provider usage and verifying the request completed; a timed-out request without a returned generation ID may require an OpenRouter account usage audit. Do not edit daily counters directly. This conservative policy can reject AI requests before `/usage` shows 20,000 confirmed tokens. `/usage` separately reports reservations and available capacity.

## Official references checked during implementation

Checked 2026-10-05; catalog availability and model lifecycle can change. The OpenAI model page currently marks GPT-5.4 Nano deprecated, while OpenRouter still lists it. The requested model is preserved; use `python -m scripts.check_models` before deployment and monitor availability.

- [OpenRouter GPT-5.4 Nano catalog](https://openrouter.ai/openai/gpt-5.4-nano)
- [OpenRouter GPT-5.4 advisor catalog](https://openrouter.ai/openai/gpt-5.4)
- [OpenRouter structured outputs](https://openrouter.ai/docs/guides/features/structured-outputs)
- [OpenRouter API request/usage fields](https://openrouter.ai/docs/api_reference/overview)
- [OpenRouter usage accounting](https://openrouter.ai/docs/cookbook/administration/usage-accounting)
- [OpenRouter image inputs](https://openrouter.ai/docs/guides/overview/multimodal/image-understanding)
- [OpenRouter provider routing](https://openrouter.ai/docs/guides/routing/provider-selection)
- [OpenAI image token calculation](https://developers.openai.com/api/docs/guides/images-vision)
- [OpenAI token counting](https://developers.openai.com/api/docs/guides/token-counting)
- [GPT-5.4 Nano lifecycle](https://developers.openai.com/api/docs/models/gpt-5.4-nano)
- [LangGraph StateGraph API](https://docs.langchain.com/oss/python/langgraph/graph-api)
- [LangChain typed tools](https://docs.langchain.com/oss/python/langchain/tools)
- [Telegram Bot API, webhook secret and files](https://core.telegram.org/bots/api)
- [SQLAlchemy async sessions](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html)
- [Alembic async migration cookbook](https://alembic.sqlalchemy.org/en/latest/cookbook.html#using-asyncio-with-alembic)
- [Pydantic strict validation](https://docs.pydantic.dev/latest/concepts/strict_mode/)
- [FastAPI lifespan](https://fastapi.tiangolo.com/advanced/events/)
- [HTTPX timeout semantics](https://www.python-httpx.org/advanced/timeouts/)

Dependency versions were resolved together on Python 3.11 and frozen in requirements.txt; requirements.in records update bounds. Keep lock updates deliberate, rerun policy/integration tests and stage model evaluations before rollout.

Additional runtime references: [Pillow decoding and decompression protection](https://pillow.readthedocs.io/en/stable/reference/Image.html), [Docker multi-stage builds](https://docs.docker.com/build/building/multi-stage/), [Compose dependency readiness](https://docs.docker.com/compose/how-tos/startup-order/), and [pytest-asyncio configuration](https://pytest-asyncio.readthedocs.io/en/stable/reference/configuration.html).

## Opening-balance extension (2026-10-06)

A current-money statement is a starting balance, not income. Revision 0003 adds one immutable opening balance per user, linked to an owner-confirmed pending operation. Explicit Indonesian/English current-money patterns and `/opening` are deterministically parsed. Creation and confirmation both recheck the policy while holding the same user-row lock used for ledger writes: no previous opening, no committed income/expense. Duplicate callbacks cannot add money twice. Transactions dated before the opening baseline are rejected at creation and confirmation. A confirmed opening survives month boundaries; ordinary monthly reports continue to exclude it from income. Initializing accounts with existing transactions or reconciling an already-initialized balance is intentionally unsupported until an explicit, auditable adjustment flow is designed. Downgrade refuses to delete opening data or pending audit records.

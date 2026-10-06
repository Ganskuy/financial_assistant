from datetime import date

from langsmith import tracing_context

from app.agents.graphs.workflows import (
    advisor_graph,
    confirmation_graph,
    receipt_graph,
    transaction_graph,
    visualization_graph,
)
from app.agents.nodes.finance import FinanceNodes
from app.agents.nodes.visualization import VisualizationNodes
from app.core.errors import InvalidInput
from app.schemas.telegram import Update
from app.services.finance import FinanceService, format_balance, format_summary
from app.services.pending import PendingService
from app.services.validation import idr, month_bounds, parse_idr, parse_opening_idr, today
from app.telegram.router import route_text

HELP = """Personal finance assistant (IDR)
Send a receipt photo or a clear expense/income, e.g.:
Tadi beli kopi 25 ribu di Point Coffee
Aku dapat gaji 7 juta
To start: Saat ini aku memiliki uang sebanyak 813.794 rupiah
Every change requires Confirm. Nothing is saved from extraction alone.

/opening AMOUNT — initialize your current money once, before transactions
/balance — current tracked balance including opening money
/balance YYYY-MM — monthly income minus expenses
/report [YYYY-MM] — category report, no AI
/visualize MONTH [YEAR] — monthly charts from confirmed records
/history — latest 10 entries
/usage — global AI token allowance
/advice [YYYY-MM] — grounded guidance
/budget — current budget status
/budget YYYY-MM | food | 1 juta — propose budget
/goal Emergency fund | 10 juta | 2027-12-31 — propose goal
/save Emergency fund | 500 ribu — propose savings earmark
/savings — goal progress; earmarks do not alter cash balance
/pending — reopen up to five active confirmations
/help — this help

Categories: food, transport, housing, utilities, health, shopping, education, entertainment, salary, gift, other.
Select Edit, then reply with field=value lines. Use Cancel to discard.
"""


class BotService:
    def __init__(self, db, settings, llm, budget, telegram, updates):
        self.db, self.settings, self.budget = db, settings, budget
        self.telegram, self.updates = telegram, updates
        nodes = FinanceNodes(db, settings, llm)
        self.receipt = receipt_graph(nodes)
        self.transaction = transaction_graph(nodes)
        self.advisor = advisor_graph(nodes)
        self.confirmation = confirmation_graph(nodes)
        self.visualization = visualization_graph(VisualizationNodes(db, settings, llm))

    async def process(self, update: Update) -> dict:
        with tracing_context(enabled=False):
            return await self._process(update)

    async def _process(self, update: Update) -> dict:
        telegram_id, _, message_id = update.identity()
        if telegram_id not in self.settings.allowed_users:
            raise InvalidInput("This account is not authorized.")
        user_id = await self.updates.user(telegram_id)
        pending = PendingService(self.db, self.settings, user_id)
        finance = FinanceService(self.db, user_id)
        state = {"user_id": user_id, "update_id": update.update_id, "message_id": message_id}
        if update.callback_query:
            token = update.callback_query.data
            if not token:
                raise InvalidInput("Unsupported callback.")
            return (await self.confirmation.ainvoke({**state, "callback_token": token}))["response"]
        message = update.message
        if message is None:
            raise InvalidInput("Unsupported message.")
        # A crash after staging but before notifying must not run extraction again.
        existing = await pending.existing(update.update_id)
        if existing:
            if existing.status == "confirmed":
                return {"text": "This operation has already been confirmed."}
            return await pending.preview(existing.id)
        if message.photo or message.document:
            file = (
                max(message.photo, key=lambda p: p.width * p.height)
                if message.photo
                else message.document
            )
            image = await self.telegram.receipt(
                file.file_id, file.file_size, getattr(file, "mime_type", None)
            )
            return (
                await self.receipt.ainvoke({**state, "image": image, "input_source": "receipt"})
            )["response"]
        text = message.text or ""
        if not text:
            raise InvalidInput("Send text or a JPEG/PNG receipt photo.")
        route = route_text(text)
        if route.name == "extract":
            editing = await pending.editing()
            if editing:
                return (
                    await self.confirmation.ainvoke(
                        {**state, "pending_id": editing.id, "edit_text": text}
                    )
                )["response"]
            return (
                await self.transaction.ainvoke({**state, "text": text, "input_source": "text"})
            )["response"]
        if route.name in {"help", "start"}:
            return {"text": HELP}
        if route.name == "visualize":
            return (await self.visualization.ainvoke({**state, "text": route.args}))["response"]
        if route.name == "opening":
            if not route.args:
                return {
                    "text": "Set your opening balance before recording income/expenses. Example: /opening 813794. Confirmation is required; this is not income."
                }
            item = await pending.create(
                "opening",
                {"amount": parse_opening_idr(route.args), "as_of": today().isoformat()},
                update.update_id,
                message_id,
                "text" if not text.startswith("/") else "command",
            )
            return await pending.preview(item.id)
        if route.name == "balance" and not route.args:
            return {"text": format_balance(await finance.current_balance())}
        if route.name in {"balance", "report"}:
            return {"text": format_summary(await finance.summary(route.args or None))}
        if route.name == "history":
            rows = await finance.recent()
            return {
                "text": "Transaction history\n"
                + (
                    "\n".join(
                        f"{r['date']} {r['type']} {idr(r['amount'])} [{r['category']}] {r['description']}"
                        for r in rows
                    )
                    or "No transactions."
                )
            }
        if route.name == "usage":
            usage = await self.budget.usage()
            return {
                "text": f"Daily AI usage ({usage['day']})\nUsed: {usage['used']:,} / {usage['limit']:,} tokens\nReserved/uncertain: {usage['reserved']:,}\nRemaining available: {usage['remaining']:,}\nReset: 00:00 Asia/Jakarta\nUnresolved calls retain their allowance until audited."
            }
        if route.name == "advice":
            period = route.args or route.period
            month_bounds(period)
            return (await self.advisor.ainvoke({**state, "period": period}))["response"]
        if route.name == "pending":
            ids = await pending.active()
            if not ids:
                return {"text": "No active pending operations."}
            previews = [await pending.preview(p) for p in ids]
            # Worker sends these extra previews durably together with its response.
            return {"text": "Active pending operations:", "additional": previews}
        if route.name == "savings":
            rows = await finance.savings()
            return {
                "text": "Savings goals\n"
                + (
                    "\n".join(
                        f"{r['name']}: {idr(r['saved'])} / {idr(r['target'])}, deadline {r['deadline']}"
                        for r in rows
                    )
                    or "No goals. Use /goal Name | amount | YYYY-MM-DD."
                )
            }
        if route.name == "budget" and not route.args:
            rows = await finance.budgets()
            return {
                "text": "Monthly budgets\n"
                + (
                    "\n".join(
                        f"{r['category']}: {idr(r['spent'])} / {idr(r['limit'])}; remaining {idr(r['remaining'])}"
                        for r in rows
                    )
                    or "No budgets. Use /budget YYYY-MM | category | amount."
                )
            }
        if route.name in {"budget", "goal", "save"}:
            parts = [part.strip() for part in route.args.split("|")]
            try:
                if route.name == "budget" and len(parts) == 3:
                    start, _ = month_bounds(parts[0])
                    kind, data = (
                        "budget",
                        {"month": str(start), "category": parts[1], "amount": parse_idr(parts[2])},
                    )
                elif route.name == "goal" and len(parts) == 3:
                    kind, data = (
                        "goal",
                        {
                            "name": parts[0],
                            "target": parse_idr(parts[1]),
                            "deadline": str(date.fromisoformat(parts[2])),
                        },
                    )
                elif route.name == "save" and len(parts) == 2:
                    kind, data = "saving", {"name": parts[0], "amount": parse_idr(parts[1])}
                else:
                    raise ValueError("Invalid arguments")
            except ValueError as exc:
                raise InvalidInput("Invalid command format. See /help for examples.") from exc
            item = await pending.create(kind, data, update.update_id, message_id, "command")
            return await pending.preview(item.id)
        return {"text": "Unsupported command. Use /help."}

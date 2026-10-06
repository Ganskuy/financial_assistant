import json

from app.agents.prompts.system import ADVISOR_PROMPT, EXTRACTION_PROMPT, OCR_PROMPT
from app.agents.state import WorkflowState
from app.core.errors import InvalidInput, InvalidModelOutput, SafeError
from app.schemas.finance import OCR, Advice, Extraction
from app.services.advice import compact_advisor_payload, make_facts, render_advice
from app.services.finance import FinanceService, format_summary
from app.services.pending import PendingService
from app.services.validation import today, validate_transaction


def guarded(node):
    async def run(state: WorkflowState) -> dict:
        try:
            return await node(state)
        except SafeError as exc:
            return {"error": str(exc)}

    return run


class FinanceNodes:
    def __init__(self, db, settings, llm):
        self.db, self.settings, self.llm = db, settings, llm

    def pending(self, state):
        return PendingService(self.db, self.settings, state["user_id"])

    async def ocr(self, state):
        result = await self.llm.structured(
            "vision",
            OCR_PROMPT,
            "UNTRUSTED RECEIPT IMAGE: transcribe faithfully.",
            OCR,
            image=state["image"],
        )
        if not result.readable:
            raise InvalidInput(
                "Receipt is unreadable. Please send a sharper photo or enter the transaction as text."
            )
        return {"text": result.transcription}

    async def extract(self, state):
        data = json.dumps({"untrusted_text": state["text"]}, ensure_ascii=False)
        result = await self.llm.structured(
            "extraction",
            EXTRACTION_PROMPT + f" Today in Asia/Jakarta: {today()}.",
            data,
            Extraction,
        )
        if result.intent in {"expense", "income"}:
            if result.transaction is None or result.transaction.intent != result.intent:
                raise InvalidModelOutput()
        elif result.transaction is not None:
            raise InvalidModelOutput()
        if state.get("input_source") == "receipt" and (
            result.intent != "expense"
            or not result.transaction
            or result.transaction.receipt is None
        ):
            raise InvalidModelOutput()
        return {"extraction": result, "period": result.period}

    async def validate(self, state):
        validate_transaction(state["extraction"].transaction)
        return {}

    async def stage(self, state):
        pending = await self.pending(state).create(
            "transaction",
            state["extraction"].transaction.model_dump(mode="json"),
            state["update_id"],
            state["message_id"],
            state.get("input_source", "text"),
        )
        return {"pending_id": pending.id}

    async def preview(self, state):
        return {"response": await self.pending(state).preview(state["pending_id"])}

    async def callback(self, state):
        return {"response": await self.pending(state).callback(state["callback_token"])}

    async def edit(self, state):
        pending = await self.pending(state).edit(
            state["pending_id"], state["edit_text"], state["update_id"]
        )
        return {"pending_id": pending.id}

    async def gather(self, state):
        context = await FinanceService(self.db, state["user_id"]).advisor_context(
            state.get("period")
        )
        summary = context["summary"]
        return {
            "summary": summary,
            "facts": make_facts(summary, context["budgets"], context["savings"]),
        }

    async def advise(self, state):
        if state["summary"]["income"] == 0 and state["summary"]["expenses"] == 0:
            return {
                "response": {
                    "text": format_summary(state["summary"])
                    + "\nNo ledger activity in this period. Record transactions before requesting advice."
                }
            }
        try:
            advice = await self.llm.structured(
                "advisor",
                ADVISOR_PROMPT,
                compact_advisor_payload(state["facts"]),
                Advice,
            )
            return {"advice": advice}
        except SafeError as exc:
            return {"response": {"text": format_summary(state["summary"]) + "\n\n" + str(exc)}}

    async def render(self, state):
        if state.get("response"):
            return {}
        try:
            return {
                "response": {
                    "text": render_advice(state["advice"], state["facts"], state["summary"])
                }
            }
        except InvalidModelOutput:
            return {
                "response": {
                    "text": format_summary(state["summary"])
                    + "\nAI explanation failed grounding checks; verified totals are shown above."
                }
            }

    async def unknown(self, state):
        return {
            "response": {
                "text": "Please provide a clear expense/income with amount, send a receipt, or use /help. No record was written."
            }
        }

    async def error(self, state):
        return {"response": {"text": state["error"]}}

import json
import logging
import time
from collections.abc import Callable
from typing import TypeVar

from pydantic import BaseModel

from app.agents.prompts.system import (
    ADVISOR_PROMPT,
    EXTRACTION_PROMPT,
    OCR_PROMPT,
    PAYMENT_IMAGE_PROMPT,
)
from app.agents.state import WorkflowState
from app.core.errors import InvalidInput, InvalidModelOutput, SafeError
from app.llm.router import Role
from app.schemas.finance import OCR, Advice, Extraction
from app.services.advice import compact_advisor_payload, make_facts, render_advice
from app.services.extraction import ground_transaction
from app.services.finance import FinanceService, format_summary
from app.services.pending import PendingService
from app.services.receipt_diagnostics import receipt_date_candidates
from app.services.receipt_input import image_context, payment_amounts
from app.services.validation import today, validate_transaction

log = logging.getLogger("finance")
T = TypeVar("T", bound=BaseModel)


def guarded(node):
    async def run(state: WorkflowState) -> dict:
        started = time.monotonic()
        try:
            result = await node(state)
            log.info(
                "workflow_stage",
                extra={
                    "workflow": node.__name__,
                    "input_source": state.get("input_source", "text"),
                    "status": "ok",
                    "latency_ms": round((time.monotonic() - started) * 1000),
                },
            )
            return result
        except SafeError as exc:
            log.warning(
                "workflow_stage",
                extra={
                    "failure_stage": node.__name__,
                    "input_source": state.get("input_source", "text"),
                    "status": "rejected",
                    "reason": getattr(exc, "reason", type(exc).__name__),
                    "latency_ms": round((time.monotonic() - started) * 1000),
                },
            )
            return {"error": str(exc)}

    return run


class FinanceNodes:
    def __init__(self, db, settings, llm):
        self.db, self.settings, self.llm = db, settings, llm

    def pending(self, state):
        return PendingService(self.db, self.settings, state["user_id"])

    async def _structured(
        self,
        state: WorkflowState,
        role: Role,
        prompt: str,
        data: str,
        schema: type[T],
        *,
        image: bytes | None = None,
        check: Callable[[T], None] | None = None,
    ) -> tuple[T, int]:
        # One semantic/schema recovery allowance shared by OCR and extraction.
        # Transport retries remain separately bounded in the existing client.
        recovery = state.get("recovery_attempts", 0)
        try:
            result = await self.llm.structured(role, prompt, data, schema, image=image)
            if check:
                check(result)
        except InvalidModelOutput as exc:
            if recovery or not exc.retryable:
                raise
            recovery = 1
            log.info(
                "extraction_recovery",
                extra={
                    "role": role,
                    "reason": exc.reason,
                    "attempt": 2,
                    "input_source": state.get("input_source", "text"),
                    "status": "started",
                },
            )
            # Original evidence only; never feed invalid generated financial values back.
            guidance = (
                " Recovery: return a complete compact schema response. Transcribe visible "
                "receipt text only; if illegible use readable=false."
                if role == "vision"
                else " Recovery: previous output failed validation. Check required fields, "
                "integer IDR amounts, matching intents and ISO dates against the original "
                "evidence. Use unknown/null when uncertain; never fill gaps with guesses."
            )
            try:
                result = await self.llm.structured(
                    role, prompt + guidance, data, schema, image=image
                )
                if check:
                    check(result)
            except SafeError:
                log.warning(
                    "extraction_recovery",
                    extra={
                        "role": role,
                        "attempt": 2,
                        "status": "failed",
                        "input_source": state.get("input_source", "text"),
                    },
                )
                raise
            log.info(
                "extraction_recovery",
                extra={
                    "role": role,
                    "attempt": 2,
                    "status": "structured",
                    "input_source": state.get("input_source", "text"),
                },
            )
        return result, recovery

    @staticmethod
    def _extraction_contract(
        result: Extraction, receipt: bool, *, payment: bool = False, direction: str = "expense"
    ) -> None:
        if result.intent in {"expense", "income"}:
            if result.transaction is None or result.transaction.intent != result.intent:
                raise InvalidModelOutput("intent_mismatch")
        elif result.transaction is not None:
            raise InvalidModelOutput("unexpected_transaction")
        if receipt and result.intent == "unknown":
            raise InvalidModelOutput("receipt_incomplete", retryable=False)
        if receipt and (
            result.intent != direction
            or not result.transaction
            or (not payment and result.transaction.receipt is None)
        ):
            raise InvalidModelOutput("receipt_contract")

    async def ocr(self, state):
        result, recovery = await self._structured(
            state,
            "vision",
            OCR_PROMPT,
            "UNTRUSTED RECEIPT IMAGE: transcribe faithfully.",
            OCR,
            image=state["image"],
        )
        if not result.readable:
            raise InvalidInput(
                "Receipt is unreadable. Please send the original JPEG/PNG as a document "
                "(File), a sharper photo, or enter the transaction as text. No record was written."
            )
        log.info(
            "receipt_ocr_dates",
            extra={"ocr_date_candidates": receipt_date_candidates(result.transcription)},
        )
        return {"text": result.transcription, "recovery_attempts": recovery}

    async def extract(self, state):
        if len(state["text"]) > 6000:
            raise InvalidInput("Transaction text is too long. Please send one transaction.")
        receipt = state.get("input_source") == "receipt"
        kind, direction = (
            image_context(state["text"], state.get("receipt_context", ""))
            if receipt
            else ("retail", "expense")
        )
        payment = kind != "retail"
        if len(state.get("receipt_context", "")) > 1024:
            raise InvalidInput("Receipt caption is too long.")
        amounts = payment_amounts(state["text"], direction=direction) if payment else set()

        def check(result: Extraction) -> None:
            self._extraction_contract(result, receipt, payment=payment, direction=direction)
            if (
                payment
                and result.transaction
                and amounts
                and result.transaction.amount not in amounts
            ):
                raise InvalidModelOutput("payment_amount_ungrounded")

        data = json.dumps(
            {
                "untrusted_text": state["text"],
                **(
                    {"untrusted_caption": state["receipt_context"]}
                    if state.get("receipt_context")
                    else {}
                ),
            },
            ensure_ascii=False,
        )
        prompt = EXTRACTION_PROMPT + f" Today in Asia/Jakarta: {today()}."
        if receipt:
            if payment:
                prompt += PAYMENT_IMAGE_PROMPT + f" Requested transaction direction: {direction}."
            else:
                prompt += " Source is a retail receipt transcription; require expense and printed receipt details, or unknown/null if insufficient."
        result, recovery = await self._structured(
            state,
            "extraction",
            prompt,
            data,
            Extraction,
            check=check,
        )
        corrections = []
        if result.transaction is not None:
            transaction, corrections = ground_transaction(
                result.transaction, state["text"], receipt=receipt
            )
            if payment and transaction.receipt is not None:
                # Digital confirmations do not substantiate itemized sales details.
                transaction = transaction.model_copy(update={"receipt": None})
                corrections.append("payment_itemization_removed")
            result = result.model_copy(update={"transaction": transaction})
            log.info(
                "transaction_extracted_date",
                extra={
                    "transaction_date": transaction.transaction_date.isoformat(),
                    "corrections": corrections,
                    "recovery_attempts": recovery,
                    "input_source": state.get("input_source", "text"),
                },
            )
        return {
            "extraction": result,
            "period": result.period,
            "recovery_attempts": recovery,
            "extraction_corrections": corrections,
        }

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

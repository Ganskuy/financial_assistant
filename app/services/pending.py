import secrets
from datetime import UTC, date, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.db import Database
from app.core.errors import InvalidInput, NotFound
from app.models.tables import (
    Budget,
    CallbackAction,
    OpeningBalance,
    PendingTransaction,
    SavingsContribution,
    SavingsGoal,
    Transaction,
    TransactionItem,
    TransactionRemoval,
    User,
    active_transaction,
)
from app.repositories.finance import FinanceRepository
from app.schemas.finance import (
    BudgetDraft,
    GoalDraft,
    OpeningDraft,
    RemovalDraft,
    SavingDraft,
    TransactionDraft,
)
from app.services.validation import idr, parse_idr, parse_opening_idr, today, validate_transaction

SCHEMAS = {
    "transaction": TransactionDraft,
    "budget": BudgetDraft,
    "goal": GoalDraft,
    "saving": SavingDraft,
    "opening": OpeningDraft,
    "removal": RemovalDraft,
}
EDITABLE = {
    "transaction": {"amount", "transaction_date", "merchant", "description", "category"},
    "budget": {"amount", "category", "month"},
    "goal": {"name", "target", "deadline"},
    "saving": {"name", "amount"},
    "opening": {"amount"},
}


class PendingService:
    def __init__(self, db: Database, settings: Settings, user_id: UUID):
        self.db, self.settings, self.user_id = db, settings, user_id

    def validate(self, kind: str, payload: dict) -> tuple[dict, list[str]]:
        if kind not in SCHEMAS:
            raise InvalidInput("Unsupported operation.")
        try:
            parsed = SCHEMAS[kind].model_validate(payload)
        except ValidationError as exc:
            raise InvalidInput(
                "Invalid fields. Check amount, date, category and required values."
            ) from exc
        warnings = validate_transaction(parsed) if isinstance(parsed, TransactionDraft) else []
        if isinstance(parsed, GoalDraft) and not today() <= parsed.deadline <= date(
            today().year + 30, 12, 31
        ):
            raise InvalidInput("Savings deadline must be today or within the next 30 years.")
        if isinstance(parsed, BudgetDraft) and not 2000 <= parsed.month.year <= today().year + 5:
            raise InvalidInput("Unsupported budget year.")
        if isinstance(parsed, OpeningDraft) and not date(2000, 1, 1) <= parsed.as_of <= today():
            raise InvalidInput("Invalid opening balance date.")
        return parsed.model_dump(mode="json"), warnings

    async def existing(self, update_id: int) -> PendingTransaction | None:
        async with self.db.transaction() as session:
            return await session.scalar(
                select(PendingTransaction).where(
                    PendingTransaction.user_id == self.user_id,
                    or_(
                        PendingTransaction.source_update_id == update_id,
                        PendingTransaction.last_edit_update_id == update_id,
                    ),
                )
            )

    async def create(
        self, kind: str, payload: dict, update_id: int, message_id: int, source: str
    ) -> PendingTransaction:
        payload, warnings = self.validate(kind, payload)
        async with self.db.transaction() as session:
            # Serializes pending/edit state changes for one user, even across API replicas.
            await session.scalar(select(User).where(User.id == self.user_id).with_for_update())
            existing = await session.scalar(
                select(PendingTransaction).where(
                    PendingTransaction.user_id == self.user_id,
                    PendingTransaction.source_update_id == update_id,
                )
            )
            if existing:
                return existing
            await self._check_opening_policy(session, kind, payload)
            if kind == "transaction":
                duplicate = await session.scalar(
                    select(Transaction.id)
                    .where(
                        Transaction.user_id == self.user_id,
                        active_transaction(),
                        Transaction.transaction_date
                        == date.fromisoformat(payload["transaction_date"]),
                        Transaction.amount == payload["amount"],
                        Transaction.type == payload["intent"],
                        Transaction.merchant_or_source == payload["merchant"],
                    )
                    .limit(1)
                )
                if duplicate:
                    warnings.append(
                        "A similar transaction already exists. Confirm only if this is a separate purchase/payment."
                    )
            pending = PendingTransaction(
                user_id=self.user_id,
                source_update_id=update_id,
                telegram_message_id=message_id,
                kind=kind,
                payload=payload,
                warnings=warnings,
                input_source=source,
                expires_at=datetime.now(UTC)
                + timedelta(minutes=self.settings.pending_transaction_ttl_minutes),
            )
            session.add(pending)
            await session.flush()
            return pending

    async def preview(self, pending_id: UUID) -> dict:
        async with self.db.transaction() as session:
            p = await session.scalar(
                select(PendingTransaction)
                .where(
                    PendingTransaction.id == pending_id, PendingTransaction.user_id == self.user_id
                )
                .with_for_update()
            )
            self._active(p)
            if p.status == "editing":
                p.status = "pending"
                p.version += 1
            return await self._preview(session, p)

    async def removal_choices(self, kind: str, update_id: int, message_id: int) -> dict:
        async with self.db.transaction() as session:
            rows = await FinanceRepository(session, self.user_id).recent(kind=kind)
        if not rows:
            return {"text": f"No {kind} entries to remove."}
        p = await self.create(
            "removal",
            {"type": kind, "candidates": [str(r.id) for r in rows]},
            update_id,
            message_id,
            "command",
        )
        return await self.preview(p.id)

    async def _removal_target(
        self, session: AsyncSession, p: PendingTransaction, target_id: str | UUID | None
    ) -> Transaction:
        if not target_id or str(target_id) not in p.payload["candidates"]:
            raise NotFound()
        row = await session.scalar(
            select(Transaction).where(
                Transaction.id == UUID(str(target_id)),
                Transaction.user_id == self.user_id,
                Transaction.type == p.payload["type"],
                active_transaction(),
            )
        )
        if row is None:
            raise InvalidInput(
                "This entry has already been removed or is unavailable. Use /remove again."
            )
        return row

    async def _preview(self, session: AsyncSession, p: PendingTransaction) -> dict:
        def button(action: str, label: str | None = None, target: UUID | None = None) -> dict:
            token = secrets.token_urlsafe(18)
            session.add(
                CallbackAction(
                    token=token,
                    pending_id=p.id,
                    action=action,
                    version=p.version,
                    target_id=target,
                )
            )
            return {"text": label or action.title(), "callback_data": token}

        if p.kind == "removal":
            if p.payload.get("selected"):
                try:
                    row = await self._removal_target(session, p, p.payload["selected"])
                except InvalidInput:
                    return {
                        "text": "This entry has already been removed or is unavailable. Cancel this request and use /remove again.",
                        "reply_markup": {"inline_keyboard": [[button("cancel")]]},
                    }
                text = (
                    "Remove this entry?\n"
                    + self._entry_text(row, full=True)
                    + "\nConfirm removes it from history, balances, reports, budgets and charts. "
                    "An internal audit record is retained."
                )
                keyboard = [[button("confirm"), button("cancel")]]
            else:
                rows = (
                    await session.scalars(
                        select(Transaction).where(
                            Transaction.id.in_([UUID(i) for i in p.payload["candidates"]]),
                            Transaction.user_id == self.user_id,
                            Transaction.type == p.payload["type"],
                            active_transaction(),
                        )
                    )
                ).all()
                by_id = {str(row.id): row for row in rows}
                text = f"Choose a {p.payload['type']} to remove (latest 10 when requested):"
                keyboard = []
                for i, target in enumerate(p.payload["candidates"], 1):
                    if target in by_id:
                        row = by_id[target]
                        text += f"\n{i}. {self._entry_text(row)}"
                        keyboard.append([button("select", f"Remove #{i}", row.id)])
                if not rows:
                    text += "\nNo remaining entries in this selection. Use /remove again."
                keyboard.append([button("cancel")])
            text += f"\nExpires: {p.expires_at.astimezone(ZoneInfo('Asia/Jakarta')).isoformat()}"
            return {"text": text, "reply_markup": {"inline_keyboard": keyboard}}
        buttons = [button(action) for action in ("confirm", "edit", "cancel")]
        return {"text": preview_text(p), "reply_markup": {"inline_keyboard": [buttons]}}

    @staticmethod
    def _entry_text(row: Transaction, *, full: bool = False) -> str:
        # Bound list text; the Telegram client chunks long messages safely.
        return (
            f"{row.transaction_date} {row.type} {idr(row.amount)} [{row.category}]"
            f" — {(row.merchant_or_source or '-')[: 120 if full else 60]} — {row.description[: 240 if full else 120]}"
        )

    @staticmethod
    def _active(p: PendingTransaction | None) -> None:
        if p is None or p.expires_at <= datetime.now(UTC) or p.status not in {"pending", "editing"}:
            raise NotFound()

    async def callback(self, token: str) -> dict:
        async with self.db.transaction() as session:
            await session.scalar(select(User).where(User.id == self.user_id).with_for_update())
            pair = (
                await session.execute(
                    select(PendingTransaction, CallbackAction)
                    .join(CallbackAction, CallbackAction.pending_id == PendingTransaction.id)
                    .where(
                        CallbackAction.token == token, PendingTransaction.user_id == self.user_id
                    )
                    .with_for_update(of=PendingTransaction)
                )
            ).first()
            if not pair:
                raise NotFound()
            p, action = pair
            if p.status == "confirmed":
                return {"text": "Already confirmed. No further changes were made."}
            self._active(p)
            if action.version != p.version:
                raise NotFound()
            if action.action == "cancel":
                p.status = "cancelled"
                p.version += 1
                return {"text": "Cancelled. No financial record was written."}
            if action.action == "select":
                if p.kind != "removal" or p.payload.get("selected"):
                    raise NotFound()
                row = await self._removal_target(session, p, action.target_id)
                p.payload = {**p.payload, "selected": str(row.id)}
                p.version += 1
                return await self._preview(session, p)
            if action.action == "edit":
                if p.kind == "removal":
                    raise NotFound()
                # Keep exactly one editing target per user.
                await session.execute(
                    update(PendingTransaction)
                    .where(
                        PendingTransaction.user_id == self.user_id,
                        PendingTransaction.status == "editing",
                        PendingTransaction.id != p.id,
                    )
                    .values(status="pending", version=PendingTransaction.version + 1)
                )
                p.status = "editing"
                p.version += 1
                fields = ", ".join(sorted(EDITABLE[p.kind]))
                return {
                    "text": f"Reply with field=value, one field per line. Allowed: {fields}.\nExample: amount=25000\nUse /pending to reopen previews. No record has been written."
                }
            if p.status != "pending":
                raise NotFound()
            payload, _ = self.validate(p.kind, p.payload)
            await self._commit(session, p, payload)
            p.status = "confirmed"
            p.version += 1
            return {
                "text": "Confirmed. Entry removed; balances and reports now exclude it."
                if p.kind == "removal"
                else "Confirmed and saved."
            }

    async def _commit(self, session, p: PendingTransaction, data: dict) -> None:
        await self._check_opening_policy(session, p.kind, data)
        if p.kind == "removal":
            row = await self._removal_target(session, p, data["selected"])
            session.add(
                TransactionRemoval(
                    transaction_id=row.id,
                    user_id=self.user_id,
                    pending_id=p.id,
                )
            )
        elif p.kind == "opening":
            session.add(
                OpeningBalance(
                    user_id=self.user_id,
                    pending_id=p.id,
                    amount=data["amount"],
                    as_of=date.fromisoformat(data["as_of"]),
                )
            )
        elif p.kind == "transaction":
            record = Transaction(
                user_id=self.user_id,
                pending_id=p.id,
                type=data["intent"],
                transaction_date=date.fromisoformat(data["transaction_date"]),
                merchant_or_source=data["merchant"],
                description=data["description"],
                category=data["category"],
                amount=data["amount"],
                currency=data["currency"],
                input_source=p.input_source,
                telegram_message_id=p.telegram_message_id,
            )
            session.add(record)
            await session.flush()
            for item in (data["receipt"] or {}).get("items", []):
                session.add(TransactionItem(transaction_id=record.id, **item))
        elif p.kind == "budget":
            values = dict(
                user_id=self.user_id,
                month=date.fromisoformat(data["month"]),
                category=data["category"],
                amount=data["amount"],
            )
            await session.execute(
                insert(Budget)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=[Budget.user_id, Budget.month, Budget.category],
                    set_={"amount": data["amount"]},
                )
            )
        elif p.kind == "goal":
            values = dict(
                user_id=self.user_id,
                name=data["name"],
                target=data["target"],
                deadline=date.fromisoformat(data["deadline"]),
            )
            await session.execute(
                insert(SavingsGoal)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=[SavingsGoal.user_id, SavingsGoal.name],
                    set_={"target": data["target"], "deadline": values["deadline"]},
                )
            )
        elif p.kind == "saving":
            goal = await session.scalar(
                select(SavingsGoal)
                .where(SavingsGoal.user_id == self.user_id, SavingsGoal.name == data["name"])
                .with_for_update()
            )
            if not goal:
                raise InvalidInput("Create and confirm this savings goal first.")
            session.add(
                SavingsContribution(
                    user_id=self.user_id, goal_id=goal.id, pending_id=p.id, amount=data["amount"]
                )
            )

    async def _check_opening_policy(self, session, kind: str, data: dict) -> None:
        # Caller holds the user row lock across check + commit, including ledger writes.
        if kind not in {"opening", "transaction"}:
            return
        opening = await session.scalar(
            select(OpeningBalance).where(OpeningBalance.user_id == self.user_id)
        )
        if kind == "opening":
            if opening:
                raise InvalidInput(
                    "An opening balance is already confirmed. Use /balance; record subsequent changes as income or expenses. Your balance was not overwritten."
                )
            existing = await session.scalar(
                select(Transaction.id).where(Transaction.user_id == self.user_id).limit(1)
            )
            if existing:
                raise InvalidInput(
                    "Opening balance must be set before the first income/expense. Existing transactions were preserved; adding your current money now could double-count them."
                )
        elif opening and date.fromisoformat(data["transaction_date"]) < opening.as_of:
            raise InvalidInput(
                "Transaction predates your opening balance. It may already be included in that amount, so it cannot be recorded here."
            )

    async def editing(self) -> PendingTransaction | None:
        async with self.db.transaction() as session:
            return await session.scalar(
                select(PendingTransaction).where(
                    PendingTransaction.user_id == self.user_id,
                    PendingTransaction.status == "editing",
                    PendingTransaction.expires_at > datetime.now(UTC),
                )
            )

    async def edit(
        self, pending_id: UUID, text: str, update_id: int | None = None
    ) -> PendingTransaction:
        async with self.db.transaction() as session:
            await session.scalar(select(User).where(User.id == self.user_id).with_for_update())
            p = await session.scalar(
                select(PendingTransaction)
                .where(
                    PendingTransaction.id == pending_id, PendingTransaction.user_id == self.user_id
                )
                .with_for_update()
            )
            self._active(p)
            if update_id is not None and p.last_edit_update_id == update_id:
                return p
            if p.status != "editing":
                raise NotFound()
            data = dict(p.payload)
            changes = {}
            for line in text.splitlines():
                field, sep, value = line.partition("=")
                field, value = field.strip(), value.strip()
                if not sep or field not in EDITABLE[p.kind] or field in changes or not value:
                    raise InvalidInput(
                        "Use unique allowlisted field=value lines. No other fields may be changed."
                    )
                changes[field] = (
                    (parse_opening_idr(value) if p.kind == "opening" else parse_idr(value))
                    if field in {"amount", "target"}
                    else value
                )
            if not changes:
                raise InvalidInput("Provide at least one correction.")
            data.update(changes)
            if p.kind == "transaction" and data.get("receipt") and "amount" in changes:
                raise InvalidInput(
                    "Receipt totals cannot be edited independently of their items. Cancel and submit corrected text instead."
                )
            p.payload, p.warnings = self.validate(p.kind, data)
            p.status, p.version = "pending", p.version + 1
            p.last_edit_update_id = update_id
            await session.flush()
            return p

    async def active(self) -> list[UUID]:
        async with self.db.transaction() as session:
            return list(
                (
                    await session.scalars(
                        select(PendingTransaction.id)
                        .where(
                            PendingTransaction.user_id == self.user_id,
                            PendingTransaction.status.in_(["pending", "editing"]),
                            PendingTransaction.expires_at > datetime.now(UTC),
                        )
                        .order_by(PendingTransaction.created_at.desc())
                        .limit(5)
                    )
                ).all()
            )


def preview_text(p: PendingTransaction) -> str:
    data = p.payload
    lines = [f"Pending {p.kind} — review before confirming"]
    if p.kind == "transaction":
        lines += [
            f"Type: {data['intent']}",
            f"Date: {data['transaction_date']}",
            f"Merchant/source: {data['merchant'] or '-'}",
            f"Description: {data['description']}",
            f"Category: {data['category']}",
            f"Total: {idr(data['amount'])}",
        ]
        if data.get("receipt"):
            receipt = data["receipt"]
            lines += [
                f"Items: {len(receipt['items'])}",
                f"Subtotal: {idr(receipt['subtotal'])}",
                f"Discount: {idr(receipt['discount'])}",
                f"Tax: {idr(receipt['tax'])}",
                f"Service: {idr(receipt['service_charge'])}",
            ]
    else:
        lines += [
            f"{key}: {idr(value) if key in {'amount', 'target'} else value}"
            for key, value in data.items()
        ]
        if p.kind == "opening":
            lines.append(
                "This is your starting money, not income. It can be set only once, before any transactions. Record only later income/expenses after this baseline."
            )
        if p.kind in {"goal", "budget"}:
            lines.append("Confirmation creates or replaces this named goal/category budget.")
        if p.kind == "saving":
            lines.append("Savings earmarking only; it does not change income or expense balance.")
    lines.extend(f"Warning: {warning}" for warning in p.warnings)
    lines.append(f"Expires: {p.expires_at.astimezone(ZoneInfo('Asia/Jakarta')).isoformat()}")
    return "\n".join(lines)[:3800]

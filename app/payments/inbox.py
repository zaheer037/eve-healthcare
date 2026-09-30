"""The webhook inbox (spec section 8.2): one row per event, and the gate against duplicates."""

import uuid
from datetime import datetime

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models.payment import WebhookEvent, WebhookOutcome
from app.payments.schemas import WebhookPayload


class WebhookInbox:
    def __init__(self, session: Session) -> None:
        self._session = session

    def record(self, provider: str, payload: WebhookPayload) -> uuid.UUID | None:
        """Store an event, or say that it is a duplicate. Layer 1 of the four (spec 8.2).

        `INSERT ... ON CONFLICT DO NOTHING RETURNING id` runs in the same transaction as the state
        change. A second insert of the same event waits for the first transaction, then inserts
        nothing (it committed) or inserts the row (it rolled back). The row has no payment yet:
        `payment_id` is set at the end, so this insert takes no lock on the payment (sharp edge
        29). The stored payload holds the validated fields, so it can never carry a NUL byte.
        """
        stmt = (
            insert(WebhookEvent)
            .values(
                id=uuid.uuid4(),
                provider=provider,
                event_id=payload.event_id,
                payload=payload.model_dump(mode="json"),
            )
            .on_conflict_do_nothing(constraint="uq_webhook_events_provider_event")
            .returning(WebhookEvent.id)
        )
        return self._session.scalar(stmt)

    def mark(
        self,
        event_id: uuid.UUID,
        *,
        outcome: WebhookOutcome,
        detail: str | None,
        payment_id: uuid.UUID | None,
        processed_at: datetime,
    ) -> None:
        """Set the outcome, the processing time and the payment together, as the CHECK needs."""
        stmt = (
            update(WebhookEvent)
            .where(WebhookEvent.id == event_id)
            .values(
                outcome=outcome, detail=detail, payment_id=payment_id, processed_at=processed_at
            )
        )
        self._session.execute(stmt)

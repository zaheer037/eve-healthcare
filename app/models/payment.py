"""Payments and the webhook inbox (architecture §3.3).

A payment belongs to a booking, and the booking is the aggregate root: no payment changes without
the booking's row lock (architecture §2.2). Rows are never deleted.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, str_enum


class PaymentStatus(StrEnum):
    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class WebhookOutcome(StrEnum):
    APPLIED = "APPLIED"
    IGNORED_NOOP = "IGNORED_NOOP"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    REJECTED = "REJECTED"


# A booking has at most one payment in these statuses. A FAILED payment is over, so the unique
# index below does not block a new one. No path adds one today: a failed payment makes its booking
# FAILED, which is final (ADR-09). The index and the replay rule of `POST /payments/` rest on this.
LIVE_PAYMENT_STATUSES = (PaymentStatus.PENDING, PaymentStatus.SUCCESS)
_LIVE_SQL = ", ".join(f"'{status.value}'" for status in LIVE_PAYMENT_STATUSES)


class Payment(TimestampMixin, Base):
    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint("amount BETWEEN 0.01 AND 99999999.99", name="amount"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency"),
        # A payment has a settlement time exactly when it is no longer pending.
        CheckConstraint("(status = 'PENDING') = (settled_at IS NULL)", name="settled"),
        CheckConstraint("provider_payment_id <> ''", name="provider_ref"),
        Index(
            "uq_payments_live_per_booking",
            "booking_id",
            unique=True,
            postgresql_where=text(f"status IN ({_LIVE_SQL})"),
        ),
        # One provider reference names one payment. Payments without a reference are not compared.
        Index(
            "uq_payments_provider_ref",
            "provider",
            "provider_payment_id",
            unique=True,
            postgresql_where=text("provider_payment_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # A full index: a partial one cannot serve the foreign key check (architecture §3.4).
    booking_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("bookings.id", ondelete="RESTRICT"), index=True
    )
    amount: Mapped[Decimal]
    currency: Mapped[str]
    status: Mapped[PaymentStatus] = mapped_column(
        str_enum(PaymentStatus, "status"),
        default=PaymentStatus.PENDING,
        server_default=PaymentStatus.PENDING.value,
    )
    provider: Mapped[str] = mapped_column(default="SIMULATOR", server_default="SIMULATOR")
    provider_payment_id: Mapped[str | None]
    failure_reason: Mapped[str | None]
    settled_at: Mapped[datetime | None]


class WebhookEvent(Base):
    """A payment event that a provider delivered. Every signed and valid event is stored once.

    `outcome` and `processed_at` are empty only inside the transaction that handles the event.
    `payment_id` stays empty until the handler has locked the payment.
    """

    __tablename__ = "webhook_events"
    __table_args__ = (
        UniqueConstraint("provider", "event_id", name="uq_webhook_events_provider_event"),
        CheckConstraint("(outcome IS NULL) = (processed_at IS NULL)", name="processed"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    provider: Mapped[str]
    event_id: Mapped[str]
    payment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("payments.id", ondelete="RESTRICT"), index=True
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    outcome: Mapped[WebhookOutcome | None] = mapped_column(str_enum(WebhookOutcome, "outcome"))
    detail: Mapped[str | None] = mapped_column(String(64))
    received_at: Mapped[datetime] = mapped_column(server_default=func.now())
    processed_at: Mapped[datetime | None]

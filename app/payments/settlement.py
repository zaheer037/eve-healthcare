"""Settlement (architecture §4.3): the one place that changes a payment's status.

The payment path and the webhook path both call `settle()`. The caller holds the booking's row
lock and commits. The function does no I/O, so a test walks the whole decision table without a
database.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal, Self

from app.bookings.state import transition
from app.models.booking import Booking, BookingStatus
from app.models.payment import Payment, PaymentStatus, WebhookOutcome

type TerminalStatus = Literal[PaymentStatus.SUCCESS, PaymentStatus.FAILED]


@dataclass(frozen=True, slots=True)
class PaymentResult:
    """A terminal answer from the provider: the simulator's, or one from a webhook event."""

    status: TerminalStatus
    provider_payment_id: str | None = None
    failure_reason: str | None = None


@dataclass(frozen=True, slots=True)
class Settlement:
    """What `settle()` did. It is a value that the caller stores and reports, not a failure."""

    outcome: WebhookOutcome
    detail: str | None = None

    @classmethod
    def applied(cls) -> Self:
        return cls(WebhookOutcome.APPLIED)

    @classmethod
    def noop(cls) -> Self:
        return cls(WebhookOutcome.IGNORED_NOOP)

    @classmethod
    def review(cls, detail: str) -> Self:
        return cls(WebhookOutcome.NEEDS_REVIEW, detail)

    @property
    def needs_attention(self) -> bool:
        """An operator must look at it: the money and the booking may disagree."""
        return self.outcome is WebhookOutcome.NEEDS_REVIEW


def settle(
    booking: Booking, payment: Payment, result: PaymentResult, *, now: datetime
) -> Settlement:
    """Apply a terminal provider result to a payment and its booking.

    Each branch below is a row of the table in architecture §4.3. A repeated result changes
    nothing, and a contradicting one is left for an operator (`NEEDS_REVIEW`).
    """
    if payment.status is result.status:
        if payment.provider_payment_id is None and result.provider_payment_id is not None:
            payment.provider_payment_id = result.provider_payment_id  # record the first reference
            return Settlement.applied()
        same_reference = result.provider_payment_id in (None, payment.provider_payment_id)
        return Settlement.noop() if same_reference else Settlement.review("reference_mismatch")
    if payment.status is not PaymentStatus.PENDING:
        return Settlement.review("terminal_conflict")

    payment.status = result.status
    payment.provider_payment_id = result.provider_payment_id
    payment.failure_reason = result.failure_reason
    payment.settled_at = now

    succeeded = result.status is PaymentStatus.SUCCESS
    if booking.status is BookingStatus.PENDING:
        booking.status = transition(
            booking.status, BookingStatus.CONFIRMED if succeeded else BookingStatus.FAILED
        )
        return Settlement.applied()
    # Unreachable while the cancel guard holds: a PENDING payment keeps its booking PENDING.
    return Settlement.review("booking_not_pending") if succeeded else Settlement.applied()


def event_mismatch(
    *,
    stored_amount: Decimal,
    stored_currency: str,
    amount: Decimal | None,
    currency: str | None,
) -> str | None:
    """The detail code of an event that does not describe the payment it names, or `None`.

    An event that carries no amount or currency says nothing to compare. The amount is judged
    before the currency. A mismatch stores the event as `REJECTED` and changes nothing else.
    """
    if amount is not None and amount != stored_amount:
        return "amount_mismatch"
    if currency is not None and currency != stored_currency:
        return "currency_mismatch"
    return None

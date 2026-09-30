"""Payment use cases. The service owns the transaction and ends it before it returns."""

import uuid
from dataclasses import dataclass

import structlog
from sqlalchemy.orm import Session

from app.bookings.policy import check_payable, owner_error
from app.bookings.repository import BookingRepository
from app.core.clock import Clock, utcnow
from app.core.config import Settings
from app.core.db import retry_transient, transaction
from app.core.errors import BookingNotFound, PaymentNotFound, SimulationNotAllowed
from app.models.booking import Booking, BookingStatus
from app.models.payment import Payment, PaymentStatus, WebhookOutcome
from app.payments.inbox import WebhookInbox
from app.payments.repository import PaymentRepository
from app.payments.schemas import WebhookAck, WebhookPayload
from app.payments.settlement import PaymentResult, Settlement, event_mismatch, settle
from app.payments.simulator import PaymentSimulator

log = structlog.get_logger()

PROVIDER = "SIMULATOR"  # ponytail: one provider -> route per provider (spec section 8.5)
# A rival transaction can take a provider reference between the check and the write. The unique
# index stops the second one, and a second run of the transaction then sees the first (spec 8.3).
RETRIED_CONSTRAINTS = frozenset({"uq_payments_provider_ref"})


@dataclass(frozen=True, slots=True)
class PaymentView:
    """A payment with the current status of its booking: what `PaymentRead` shows."""

    payment: Payment
    booking_status: BookingStatus


@dataclass(frozen=True, slots=True)
class PayOutcome:
    """The result of a pay call. `created` tells a new payment from a replay."""

    view: PaymentView
    created: bool


class PaymentService:
    def __init__(
        self,
        session: Session,
        settings: Settings,
        simulator: PaymentSimulator,
        clock: Clock = utcnow,
    ) -> None:
        self._session = session
        self._settings = settings
        self._simulator = simulator
        self._clock = clock
        self._payments = PaymentRepository(session)
        self._bookings = BookingRepository(session)
        self._inbox = WebhookInbox(session)

    def pay(
        self, *, user_id: uuid.UUID, booking_id: uuid.UUID, simulate: PaymentStatus | None
    ) -> PayOutcome:
        """Pay a booking, in one short transaction (spec section 8.1).

        It locks the user's booking and looks for a live payment first. A live payment is returned
        as it is (a replay), even for a booking that was cancelled after it succeeded. Otherwise
        the booking must be `PENDING` with an appointment ahead. The simulator answers at once, so
        no lock spans a network call, and `settle()` writes the answer on both rows. One flush
        writes the new payment and the changed booking.

        The amount always comes from the booking, and a client never sends one.

        Raises:
            SimulationNotAllowed: `simulate` is set and the setting forbids it.
            BookingNotFound: no booking has this ID.
            Forbidden: the booking belongs to another user.
            BookingNotPayable: the booking is not `PENDING`, or its appointment has started.

        Commits: yes.
        """
        if simulate is not None and not self._settings.payment_allow_simulate_override:
            raise SimulationNotAllowed("A forced payment result is not allowed here.")
        with transaction(self._session):
            booking = self._bookings.lock_owned(booking_id, user_id)
            if booking is None:
                raise owner_error(
                    exists=self._bookings.exists(booking_id),
                    not_found=BookingNotFound("No booking has this ID."),
                )
            live = self._payments.live_payment(booking.id)
            if live is not None:
                return PayOutcome(PaymentView(live, booking.status), created=False)
            now = self._clock()
            check_payable(
                booking_id=booking.id,
                status=booking.status,
                appointment_at=booking.appointment_at,
                now=now,
            )
            # Built as PENDING, so `settle()` takes its first row and writes the whole answer.
            payment = Payment(
                id=uuid.uuid4(),
                booking_id=booking.id,
                amount=booking.amount,
                currency=booking.currency,
                status=PaymentStatus.PENDING,
            )
            result = self._simulator.charge(payment.id, simulate)
            if result is not None:  # `None` is pending mode: the webhook settles it later
                settle(booking, payment, result, now=now)
            self._payments.add(payment)
        return PayOutcome(PaymentView(payment, booking.status), created=True)

    def get_owned(self, user_id: uuid.UUID, payment_id: uuid.UUID) -> PaymentView:
        """One of the user's payments with its booking's status. One statement, and a second only
        on a miss, to tell a payment of another user (403) from an unknown one (404).

        Raises:
            PaymentNotFound: no payment has this ID.
            Forbidden: the payment belongs to another user.

        Commits: no.
        """
        found = self._payments.get_owned(payment_id, user_id)
        if found is None:
            raise owner_error(
                exists=self._payments.exists(payment_id),
                not_found=PaymentNotFound("No payment has this ID."),
            )
        return PaymentView(*found)

    def handle_webhook(self, payload: WebhookPayload) -> WebhookAck:
        """Store a provider event and apply it, in one transaction (spec sections 8.1 and 8.5).

        The event is stored first, with no payment, so a duplicate is one probe of a unique index
        and takes no lock. Then the booking is locked, then the payment (the lock order), the event
        is checked against the payment's amount, currency and provider reference, and `settle()`
        applies it. The event row, the payment and the booking commit together, so a failure
        removes the event and the provider's retry runs it again.

        A business failure is an outcome and not an error: the answer is `200` and the event row
        says `REJECTED` or `NEEDS_REVIEW`. A deadlock, a serialization failure or a provider
        reference that a rival took at the same moment runs the transaction again, up to three
        times (`retry_transient`). A lock timeout is not retried: the provider's own retry is the
        right tool.

        Raises:
            ServiceUnavailable: a transient failure persisted through every attempt.

        Commits: yes.
        """
        ack = retry_transient(
            lambda: self._receive(payload), retry_on_constraints=RETRIED_CONSTRAINTS
        )
        self._log(payload, ack)  # after the commit, so a rolled-back attempt never logs
        return ack

    def _receive(self, payload: WebhookPayload) -> WebhookAck:
        """One attempt: the whole transaction."""
        with transaction(self._session):
            event_id = self._inbox.record(PROVIDER, payload)
            if event_id is None:
                return WebhookAck(status="duplicate")
            booking_id = self._payments.booking_id_of(payload.payment_id)
            if booking_id is None:
                rejected = Settlement(WebhookOutcome.REJECTED, "unknown_payment")
                return self._finish(event_id, rejected, payment_id=None)
            booking = self._bookings.lock(booking_id)
            payment = self._payments.lock(payload.payment_id)
            settlement = self._judge(booking, payment, payload)
            return self._finish(event_id, settlement, payment_id=payment.id)

    def _judge(self, booking: Booking, payment: Payment, payload: WebhookPayload) -> Settlement:
        """Reject an event that does not describe this payment. Otherwise settle it."""
        mismatch = event_mismatch(
            stored_amount=payment.amount,
            stored_currency=payment.currency,
            amount=payload.amount,
            currency=payload.currency,
        )
        reference = payload.provider_payment_id
        if (
            mismatch is None
            and reference is not None
            and self._payments.reference_held_by_another(payment, reference)
        ):
            mismatch = "provider_reference_conflict"
        if mismatch is not None:
            return Settlement(WebhookOutcome.REJECTED, mismatch)
        result = PaymentResult(payload.status, reference, payload.failure_reason)
        return settle(booking, payment, result, now=self._clock())

    def _finish(
        self, event_id: uuid.UUID, settlement: Settlement, *, payment_id: uuid.UUID | None
    ) -> WebhookAck:
        self._inbox.mark(
            event_id,
            outcome=settlement.outcome,
            detail=settlement.detail,
            payment_id=payment_id,
            processed_at=self._clock(),
        )
        return WebhookAck(status="processed", outcome=settlement.outcome, detail=settlement.detail)

    @staticmethod
    def _log(payload: WebhookPayload, ack: WebhookAck) -> None:
        """One event per delivery (spec section 10.2). Ids only: the body of an event, with its
        amount and references, never goes to the log."""
        ids = {"event_id": payload.event_id, "payment_id": str(payload.payment_id)}
        outcome = ack.outcome.value if ack.outcome else None
        if ack.status == "duplicate":
            log.warning("webhook.duplicate", **ids)
        elif ack.outcome is WebhookOutcome.NEEDS_REVIEW:
            log.error("webhook.needs_review", detail=ack.detail, **ids)
        elif ack.outcome is WebhookOutcome.REJECTED:
            # An unknown payment is a delivery that raced ahead. The others break an expectation.
            report = log.warning if ack.detail == "unknown_payment" else log.error
            report("webhook.rejected", detail=ack.detail, **ids)
        else:
            log.info("webhook.processed", outcome=outcome, **ids)

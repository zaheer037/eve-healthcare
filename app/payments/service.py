"""Payment use cases. The service owns the transaction and ends it before it returns."""

import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.bookings.policy import check_payable, owner_error
from app.bookings.repository import BookingRepository
from app.core.clock import Clock, utcnow
from app.core.config import Settings
from app.core.db import transaction
from app.core.errors import BookingNotFound, PaymentNotFound, SimulationNotAllowed
from app.models.booking import BookingStatus
from app.models.payment import Payment, PaymentStatus
from app.payments.repository import PaymentRepository
from app.payments.settlement import settle
from app.payments.simulator import PaymentSimulator


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

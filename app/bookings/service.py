"""Booking use cases. The service owns the transaction and ends it before it returns."""

import uuid
from datetime import datetime, timedelta

import structlog
from sqlalchemy.orm import Session

from app.bookings.policy import check_active_cap, check_appointment_window, owner_error
from app.bookings.repository import BookingRepository
from app.bookings.state import transition
from app.core.clock import Clock, utcnow
from app.core.config import Settings
from app.core.db import domain_errors, transaction
from app.core.errors import (
    BookingNotFound,
    CentreTestNotFound,
    DuplicateBooking,
    PaymentInProgress,
)
from app.core.pagination import PageParams
from app.models.booking import Booking, BookingStatus

log = structlog.get_logger()


class BookingService:
    def __init__(self, session: Session, settings: Settings, clock: Clock = utcnow) -> None:
        self._session = session
        self._settings = settings
        self._clock = clock
        self._repository = BookingRepository(session)

    def create(
        self,
        *,
        user_id: uuid.UUID,
        centre_id: uuid.UUID,
        test_id: uuid.UUID,
        appointment_at: datetime,
    ) -> Booking:
        """Book a test at a centre for one user. Three statements (architecture §5.4).

        The booking copies the price that it reads here, so a later price change leaves it alone.
        The cap is soft: two simultaneous requests can both pass the count. `# ponytail: soft cap
        -> lock the user row with SELECT ... FOR NO KEY UPDATE if the cap must be exact`. The
        unique index on the active slot decides between two identical requests.

        Raises:
            CentreTestNotFound: the centre does not offer this test now (unknown, unavailable or
                inactive).
            AppointmentOutOfWindow: the start is too soon or too far ahead.
            BookingLimitReached: the user already holds the maximum of active bookings.
            DuplicateBooking: the user already holds this slot.

        Commits: yes.
        """
        offering = self._repository.find_offering(centre_id, test_id)
        if offering is None:  # unknown, unavailable and inactive give one answer
            raise CentreTestNotFound("This centre does not offer this test.")
        now = self._clock()
        check_appointment_window(
            appointment_at,
            now=now,
            min_lead=timedelta(minutes=self._settings.booking_min_lead_minutes),
            max_advance=timedelta(days=self._settings.booking_max_advance_days),
        )
        check_active_cap(
            active=self._repository.count_active(user_id, now),
            cap=self._settings.booking_max_active_per_user,
        )
        booking = self._repository.add(
            user_id=user_id, offering=offering, appointment_at=appointment_at
        )
        taken = {"uq_bookings_active_slot": DuplicateBooking("You already booked this slot.")}
        with domain_errors(self._session, taken):
            self._session.commit()  # one INSERT ... RETURNING
        log.info(  # after the commit, so a rolled-back attempt never logs
            "booking.created",
            booking_id=str(booking.id),
            user_id=str(user_id),
            centre_id=str(centre_id),
            test_id=str(test_id),
        )
        return booking

    def list_for_user(self, user_id: uuid.UUID, page: PageParams) -> tuple[list[Booking], int]:
        """One page of the user's bookings and their total. Two statements: the count, the page.

        Raises: nothing.
        Commits: no.
        """
        total = self._repository.count_for_user(user_id)
        return self._repository.list_for_user(user_id, page), total

    def get_owned(self, user_id: uuid.UUID, booking_id: uuid.UUID) -> Booking:
        """The user's booking with its payments. One statement, and a second only on a miss.

        Raises:
            BookingNotFound: no booking has this ID.
            Forbidden: the booking belongs to another user.

        Commits: no.
        """
        booking = self._repository.get_owned_with_payments(booking_id, user_id)
        if booking is None:
            raise owner_error(
                exists=self._repository.exists(booking_id),
                not_found=BookingNotFound("No booking has this ID."),
            )
        return booking

    def cancel(self, *, user_id: uuid.UUID, booking_id: uuid.UUID) -> Booking:
        """Cancel the user's booking. A repeated cancel is a no-op that answers the same.

        The row lock makes the check for a pending payment and the status change one step
        against a payment or a webhook (architecture §2.2). A refund is out of scope, so a paid
        booking keeps its `SUCCESS` payment (assumption A7).

        Raises:
            BookingNotFound: no booking has this ID.
            Forbidden: the booking belongs to another user.
            InvalidBookingStatus: the booking failed, and a failed booking is final.
            PaymentInProgress: a payment for the booking is still pending.

        Commits: yes.
        """
        changed_from: BookingStatus | None = None
        with transaction(self._session):
            booking = self._repository.lock_owned(booking_id, user_id)
            if booking is None:
                raise owner_error(
                    exists=self._repository.exists(booking_id),
                    not_found=BookingNotFound("No booking has this ID."),
                )
            if booking.status is not BookingStatus.CANCELLED:
                changed_from = booking.status
                transition(booking.status, BookingStatus.CANCELLED)  # a FAILED booking stops here
                if self._repository.has_pending_payment(booking.id):
                    raise PaymentInProgress("A payment for this booking is still pending.")
                booking.status = BookingStatus.CANCELLED
            self._repository.attach_names(booking)
        if changed_from is not None:  # a repeated cancel changed nothing, so it logs nothing
            log.info(
                "booking.status_changed",
                booking_id=str(booking.id),
                user_id=str(user_id),
                from_status=changed_from.value,
                to_status=BookingStatus.CANCELLED.value,
                cause="cancel",
            )
        return booking

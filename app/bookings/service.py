"""Booking use cases. The service owns the transaction and ends it before it returns."""

import uuid
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.bookings.policy import check_active_cap, check_appointment_window
from app.bookings.repository import BookingRepository
from app.core.clock import Clock, utcnow
from app.core.config import Settings
from app.core.db import domain_errors
from app.core.errors import CentreTestNotFound, DuplicateBooking
from app.models.booking import Booking


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
        """Book a test at a centre for one user. Three statements (spec section 7.4).

        The booking copies the price that it reads here, so a later price change leaves it alone.
        The cap is soft: two simultaneous requests can both pass the count. `# ponytail: soft cap
        -> lock the user row with SELECT ... FOR NO KEY UPDATE if the cap must be exact`. The
        unique index on the active slot decides between two identical requests.
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
        return booking

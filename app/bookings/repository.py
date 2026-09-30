"""Booking queries. SQL only: no business rule and no commit (spec section 4.2)."""

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from app.models.booking import ACTIVE_STATUSES, Booking
from app.models.catalog import Centre, CentreTest, DiagnosticTest


@dataclass(frozen=True, slots=True)
class Offering:
    """A test that a centre offers now, with the price that a booking copies."""

    centre: Centre
    test: DiagnosticTest
    price: Decimal
    currency: str


class BookingRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def find_offering(self, centre_id: uuid.UUID, test_id: uuid.UUID) -> Offering | None:
        """The price row of a test that a centre offers, with the centre and the test.

        One statement over three tables. A row exists only when the test is available at the
        centre and both parents are active. Every join runs on a primary key (spec section 5.4).
        """
        stmt = (
            select(Centre, DiagnosticTest, CentreTest.price, CentreTest.currency)
            .join(CentreTest, CentreTest.centre_id == Centre.id)
            .join(DiagnosticTest, DiagnosticTest.id == CentreTest.test_id)
            .where(
                CentreTest.centre_id == centre_id,
                CentreTest.test_id == test_id,
                CentreTest.is_available,
                Centre.is_active,
                DiagnosticTest.is_active,
            )
        )
        row = self._session.execute(stmt).first()
        return Offering(*row) if row else None

    def count_active(self, user_id: uuid.UUID, now: datetime) -> int:
        """The user's bookings that hold a slot and have not started."""
        stmt = (
            select(func.count())
            .select_from(Booking)
            .where(
                Booking.user_id == user_id,
                Booking.status.in_(ACTIVE_STATUSES),
                Booking.appointment_at > now,
            )
        )
        return self._session.scalar(stmt) or 0

    def add(self, *, user_id: uuid.UUID, offering: Offering, appointment_at: datetime) -> Booking:
        """Stage a `PENDING` booking that copies the offering's price. The commit inserts it.

        The centre and the test that the offering already loaded are attached, so the response
        shows their names without another query.
        """
        booking = Booking(
            user_id=user_id,
            centre_id=offering.centre.id,
            test_id=offering.test.id,
            appointment_at=appointment_at,
            amount=offering.price,
            currency=offering.currency,
        )
        self._session.add(booking)
        set_committed_value(booking, "centre", offering.centre)
        set_committed_value(booking, "test", offering.test)
        return booking

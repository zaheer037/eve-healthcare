"""Booking queries. SQL only: no business rule and no commit (spec section 4.2)."""

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import ColumnElement, Select, exists, func, select
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.orm.attributes import set_committed_value

from app.core.pagination import PageParams
from app.models.booking import ACTIVE_STATUSES, Booking
from app.models.catalog import Centre, CentreTest, DiagnosticTest
from app.models.payment import Payment, PaymentStatus


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

    # ---------------------------------------------------------------- reads

    def count_for_user(self, user_id: uuid.UUID) -> int:
        stmt = select(func.count()).select_from(Booking).where(Booking.user_id == user_id)
        return self._session.scalar(stmt) or 0

    def list_for_user(self, user_id: uuid.UUID, page: PageParams) -> list[Booking]:
        """One page of the user's bookings, newest first, with the names of the centre and test.

        The order is that of `ix_bookings_user_created`, and the id breaks a tie in `created_at`.
        """
        stmt = (
            select(Booking)
            .where(Booking.user_id == user_id)
            .options(
                joinedload(Booking.centre, innerjoin=True),
                joinedload(Booking.test, innerjoin=True),
            )
            .order_by(Booking.created_at.desc(), Booking.id.desc())
            .limit(page.limit)
            .offset(page.offset)
        )
        return list(self._session.scalars(stmt))

    def get_owned_with_payments(self, booking_id: uuid.UUID, user_id: uuid.UUID) -> Booking | None:
        """The booking, its names and all its payments in ONE statement: one snapshot, so the
        booking status and its payments always agree. The owner predicate is in the query."""
        stmt = (
            select(Booking)
            .where(Booking.id == booking_id, Booking.user_id == user_id)
            .options(
                joinedload(Booking.centre, innerjoin=True),
                joinedload(Booking.test, innerjoin=True),
                joinedload(Booking.payments),
            )
        )
        return self._session.execute(stmt).unique().scalar_one_or_none()

    def exists(self, booking_id: uuid.UUID) -> bool:
        """A plain read that tells 403 from 404 after an owner-scoped read found nothing."""
        return bool(self._session.scalar(select(exists().where(Booking.id == booking_id))))

    # ---------------------------------------------------------------- cancel

    @staticmethod
    def _locking_select(*criteria: ColumnElement[bool]) -> Select[tuple[Booking]]:
        """The one form of a booking lock, so no caller can forget a rule of spec section 4.2:

        1. `FOR NO KEY UPDATE`, because a child insert takes `FOR KEY SHARE` on this row.
        2. `populate_existing`, or the ORM returns a stale object that is already in the session.
        3. One table, no joined loading.
        The fourth rule, the owner predicate, is the caller's criterion.
        """
        return (
            select(Booking)
            .where(*criteria)
            .with_for_update(key_share=True)
            .execution_options(populate_existing=True)
        )

    def lock_owned(self, booking_id: uuid.UUID, user_id: uuid.UUID) -> Booking | None:
        """Lock the user's booking, or return `None`.

        The owner predicate is in the lock query, so a stranger finds no row and never waits behind
        the owner's lock (sharp edge 30).
        """
        stmt = self._locking_select(Booking.id == booking_id, Booking.user_id == user_id)
        return self._session.scalar(stmt)

    def lock(self, booking_id: uuid.UUID) -> Booking | None:
        """Lock a booking by its id, for the webhook. It has no user: it found the booking
        through its own payment record and trusts nothing that the payload says about it."""
        return self._session.scalar(self._locking_select(Booking.id == booking_id))

    def has_pending_payment(self, booking_id: uuid.UUID) -> bool:
        """A separate statement after the lock, so it sees every payment that a rival committed
        before it released the lock."""
        stmt = select(
            exists().where(
                Payment.booking_id == booking_id, Payment.status == PaymentStatus.PENDING
            )
        )
        return bool(self._session.scalar(stmt))

    def attach_names(self, booking: Booking) -> None:
        """Load the centre and the test of a locked booking, for the response.

        The locking query reads one table, so the names come from one more statement. It also
        flushes the change that the service made, in the same transaction.
        """
        # Joined through `centre_tests`: the booking's composite foreign key guarantees the row.
        stmt = (
            select(Centre, DiagnosticTest)
            .join(CentreTest, CentreTest.centre_id == Centre.id)
            .join(DiagnosticTest, DiagnosticTest.id == CentreTest.test_id)
            .where(CentreTest.centre_id == booking.centre_id, CentreTest.test_id == booking.test_id)
        )
        centre, test = self._session.execute(stmt).one()
        set_committed_value(booking, "centre", centre)
        set_committed_value(booking, "test", test)

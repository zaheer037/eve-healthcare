"""BookingService on a real database: create, with the window, the cap and the duplicate guard."""

import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session

from app.bookings.service import BookingService
from app.core.config import Settings
from app.core.errors import (
    AppointmentOutOfWindow,
    BookingLimitReached,
    CentreTestNotFound,
    DuplicateBooking,
)
from app.models.booking import Booking, BookingStatus
from tests.factories import (
    make_booking,
    make_centre,
    make_centre_test,
    make_diagnostic_test,
    make_user,
)
from tests.support import QueryCounter

# A fixed clock a long way from the real one: only the window and the cap read it.
FIXED_NOW = datetime(2030, 1, 1, 9, 0, tzinfo=UTC)
ServiceFactory = Callable[..., BookingService]


@pytest.fixture
def make_service(db_engine: Engine, test_settings: Settings) -> Iterator[ServiceFactory]:
    """`make_service(cap=3, now=...)` builds a service on its own session, as a request has."""
    sessions: list[Session] = []

    def build(*, now: datetime | None = None, **setting_changes: object) -> BookingService:
        session = Session(db_engine, expire_on_commit=False)
        sessions.append(session)
        settings = test_settings.model_copy(update=setting_changes)
        clock = (lambda: now) if now else (lambda: datetime.now(UTC))
        return BookingService(session, settings, clock)

    yield build
    for session in sessions:
        session.close()


def book(
    service: BookingService,
    user_id: uuid.UUID,
    offering_centre: uuid.UUID,
    offering_test: uuid.UUID,
    when: datetime,
) -> Booking:
    return service.create(
        user_id=user_id, centre_id=offering_centre, test_id=offering_test, appointment_at=when
    )


# ==== what a booking copies


def test_a_new_booking_is_pending_and_copies_the_price_and_the_names(
    db_session: Session, make_service: ServiceFactory
) -> None:
    offering = make_centre_test(db_session, price=Decimal("350"), currency="INR")
    user = make_user(db_session)
    when = FIXED_NOW + timedelta(days=3)
    booking = book(make_service(now=FIXED_NOW), user.id, offering.centre_id, offering.test_id, when)
    assert booking.status is BookingStatus.PENDING
    assert (booking.amount, booking.currency) == (Decimal("350"), "INR")
    assert booking.user_id == user.id
    assert booking.appointment_at == when
    assert booking.created_at is not None  # filled by RETURNING, with no extra query
    # Both relationships are loaded, so reading the names runs no SQL (they are `raise_on_sql`).
    assert booking.centre.name
    assert booking.test.name


def test_a_later_price_change_leaves_the_booking_alone(
    db_session: Session, make_service: ServiceFactory
) -> None:
    offering = make_centre_test(db_session, price=Decimal("350"))
    user = make_user(db_session)
    booking = book(
        make_service(now=FIXED_NOW),
        user.id,
        offering.centre_id,
        offering.test_id,
        FIXED_NOW + timedelta(days=3),
    )
    db_session.execute(
        text("UPDATE centre_tests SET price = 999 WHERE centre_id = :c AND test_id = :t"),
        {"c": offering.centre_id, "t": offering.test_id},
    )
    db_session.commit()
    stored = db_session.scalar(select(Booking.amount).where(Booking.id == booking.id))
    assert stored == Decimal("350.00")


# ==== the offering must be bookable


def test_unknown_unoffered_unavailable_and_inactive_offerings_are_all_centre_test_not_found(
    db_session: Session, make_service: ServiceFactory
) -> None:
    user = make_user(db_session)
    service = make_service(now=FIXED_NOW)
    when = FIXED_NOW + timedelta(days=3)
    good = make_centre_test(db_session)
    unavailable = make_centre_test(db_session, is_available=False)
    inactive_centre = make_centre_test(db_session, centre=make_centre(db_session, is_active=False))
    inactive_test = make_centre_test(
        db_session, test=make_diagnostic_test(db_session, is_active=False)
    )
    not_offered = (good.centre_id, make_diagnostic_test(db_session).id)
    cases = [
        (uuid.uuid4(), good.test_id),  # unknown centre
        (good.centre_id, uuid.uuid4()),  # unknown test
        not_offered,  # both exist, this centre does not offer this test
        (unavailable.centre_id, unavailable.test_id),
        (inactive_centre.centre_id, inactive_centre.test_id),
        (inactive_test.centre_id, inactive_test.test_id),
    ]
    for centre_id, test_id in cases:
        with pytest.raises(CentreTestNotFound):
            book(service, user.id, centre_id, test_id, when)
    assert db_session.scalar(select(func.count()).select_from(Booking)) == 0


# ==== the appointment window


@pytest.mark.parametrize(
    ("offset", "allowed"),
    [
        (timedelta(minutes=29), False),
        (timedelta(minutes=30), True),
        (timedelta(days=365), True),
        (timedelta(days=365, minutes=1), False),
        (timedelta(days=-1), False),
    ],
)
def test_the_window_holds_at_its_boundaries(
    db_session: Session, make_service: ServiceFactory, offset: timedelta, allowed: bool
) -> None:
    offering = make_centre_test(db_session)
    user = make_user(db_session)
    service = make_service(now=FIXED_NOW)
    attempt = lambda: book(  # noqa: E731
        service, user.id, offering.centre_id, offering.test_id, FIXED_NOW + offset
    )
    if allowed:
        assert attempt().status is BookingStatus.PENDING
    else:
        with pytest.raises(AppointmentOutOfWindow):
            attempt()


def test_the_window_limits_are_settings(db_session: Session, make_service: ServiceFactory) -> None:
    offering = make_centre_test(db_session)
    user = make_user(db_session)
    service = make_service(now=FIXED_NOW, booking_min_lead_minutes=0, booking_max_advance_days=1)
    assert book(
        service, user.id, offering.centre_id, offering.test_id, FIXED_NOW + timedelta(minutes=1)
    )
    with pytest.raises(AppointmentOutOfWindow):
        book(
            service,
            user.id,
            offering.centre_id,
            offering.test_id,
            FIXED_NOW + timedelta(days=1, minutes=1),
        )


def test_the_extreme_datetime_is_refused_without_an_error(
    db_session: Session, make_service: ServiceFactory
) -> None:
    offering = make_centre_test(db_session)
    user = make_user(db_session)
    extreme = datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone(timedelta(hours=-14)))
    with pytest.raises(AppointmentOutOfWindow):
        book(make_service(), user.id, offering.centre_id, offering.test_id, extreme)


# ==== the cap on active bookings


def test_the_booking_over_the_cap_is_refused(
    db_session: Session, make_service: ServiceFactory
) -> None:
    user = make_user(db_session)
    offering = make_centre_test(db_session)
    when = FIXED_NOW + timedelta(days=3)
    for day in range(3):
        make_booking(
            db_session, user=user, offering=offering, appointment_at=when + timedelta(days=day)
        )
    with pytest.raises(BookingLimitReached, match="3"):
        book(
            make_service(now=FIXED_NOW, booking_max_active_per_user=3),
            user.id,
            offering.centre_id,
            offering.test_id,
            when + timedelta(days=9),
        )


def test_only_active_future_bookings_of_this_user_count_against_the_cap(
    db_session: Session, make_service: ServiceFactory
) -> None:
    user, other = make_user(db_session), make_user(db_session)
    offering = make_centre_test(db_session)
    future = FIXED_NOW + timedelta(days=3)
    make_booking(db_session, user=user, offering=offering, appointment_at=future)  # counts
    make_booking(
        db_session,
        user=user,
        offering=offering,
        appointment_at=future + timedelta(days=1),
        status=BookingStatus.CANCELLED,
    )
    make_booking(
        db_session,
        user=user,
        offering=offering,
        appointment_at=future + timedelta(days=2),
        status=BookingStatus.FAILED,
    )
    make_booking(  # already over
        db_session, user=user, offering=offering, appointment_at=FIXED_NOW - timedelta(days=2)
    )
    make_booking(  # another user's
        db_session, user=other, offering=offering, appointment_at=future + timedelta(days=4)
    )
    service = make_service(now=FIXED_NOW, booking_max_active_per_user=2)
    assert book(service, user.id, offering.centre_id, offering.test_id, future + timedelta(days=5))
    with pytest.raises(BookingLimitReached):
        book(service, user.id, offering.centre_id, offering.test_id, future + timedelta(days=6))


# ==== the duplicate guard


def test_the_same_slot_twice_is_a_duplicate_and_the_session_stays_usable(
    db_session: Session, make_service: ServiceFactory
) -> None:
    offering = make_centre_test(db_session)
    user = make_user(db_session)
    service = make_service(now=FIXED_NOW)
    when = FIXED_NOW + timedelta(days=3)
    book(service, user.id, offering.centre_id, offering.test_id, when)
    with pytest.raises(DuplicateBooking):
        book(service, user.id, offering.centre_id, offering.test_id, when)
    assert book(  # after the rollback the same service still works
        service, user.id, offering.centre_id, offering.test_id, when + timedelta(minutes=1)
    )


def test_the_same_slot_by_another_user_or_after_a_cancel_is_not_a_duplicate(
    db_session: Session, make_service: ServiceFactory
) -> None:
    offering = make_centre_test(db_session)
    first, second = make_user(db_session), make_user(db_session)
    when = FIXED_NOW + timedelta(days=3)
    held = make_booking(db_session, user=first, offering=offering, appointment_at=when)
    service = make_service(now=FIXED_NOW)
    assert book(service, second.id, offering.centre_id, offering.test_id, when)  # another user
    with pytest.raises(DuplicateBooking):
        book(service, first.id, offering.centre_id, offering.test_id, when)
    db_session.execute(
        text("UPDATE bookings SET status = 'CANCELLED' WHERE id = :id"), {"id": held.id}
    )
    db_session.commit()
    assert book(service, first.id, offering.centre_id, offering.test_id, when)  # the slot is free


# ==== the statement budget (spec section 7.2)


def test_a_booking_takes_three_statements(
    db_engine: Engine, db_session: Session, make_service: ServiceFactory
) -> None:
    """The offering, the count of active bookings, and one INSERT ... RETURNING."""
    offering = make_centre_test(db_session)
    user = make_user(db_session)
    service = make_service(now=FIXED_NOW)
    with QueryCounter(db_engine) as counter:
        book(service, user.id, offering.centre_id, offering.test_id, FIXED_NOW + timedelta(days=3))
    counter.assert_at_most(3)
    assert counter.count == 3

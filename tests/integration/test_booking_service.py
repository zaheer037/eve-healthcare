"""BookingService on a real database: create, with the window, the cap and the duplicate guard."""

import contextlib
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
    BookingNotFound,
    CentreTestNotFound,
    DuplicateBooking,
    Forbidden,
    InvalidBookingStatus,
    PaymentInProgress,
)
from app.core.pagination import PageParams
from app.models.booking import Booking, BookingStatus
from app.models.payment import PaymentStatus
from tests.factories import (
    future_slot,
    make_booking,
    make_centre,
    make_centre_test,
    make_diagnostic_test,
    make_payment,
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


# ==== list and read (T3.4)


def own_bookings(service: BookingService, user_id: uuid.UUID, **page: int) -> list[Booking]:
    return service.list_for_user(user_id, PageParams(**page))[0]


def test_the_list_holds_only_my_bookings_newest_first(
    db_session: Session, make_service: ServiceFactory
) -> None:
    me, other = make_user(db_session), make_user(db_session)
    offering = make_centre_test(db_session)
    first = make_booking(db_session, user=me, offering=offering, appointment_at=future_slot(3))
    second = make_booking(db_session, user=me, offering=offering, appointment_at=future_slot(4))
    make_booking(db_session, user=other, offering=offering, appointment_at=future_slot(5))
    listed, total = make_service().list_for_user(me.id, PageParams())
    assert [booking.id for booking in listed] == [second.id, first.id]
    assert total == 2


def test_bookings_made_in_one_transaction_are_ordered_by_id_so_a_page_never_repeats(
    db_session: Session, make_service: ServiceFactory
) -> None:
    """One transaction gives every row the same `created_at`, so the id breaks the tie."""
    user = make_user(db_session)
    offering = make_centre_test(db_session)
    db_session.add_all(
        Booking(
            user_id=user.id,
            centre_id=offering.centre_id,
            test_id=offering.test_id,
            appointment_at=future_slot(2 + day),
            amount=Decimal("10"),
            currency="INR",
        )
        for day in range(6)
    )
    db_session.commit()
    service = make_service()
    seen = [
        b.id for offset in (0, 2, 4) for b in own_bookings(service, user.id, limit=2, offset=offset)
    ]
    assert len(seen) == len(set(seen)) == 6
    assert seen == sorted(seen, reverse=True)


def test_a_page_past_the_end_is_empty_and_the_total_is_still_reported(
    db_session: Session, make_service: ServiceFactory
) -> None:
    user = make_user(db_session)
    make_booking(db_session, user=user)
    assert make_service().list_for_user(user.id, PageParams(limit=10, offset=50)) == ([], 1)


def test_a_listed_booking_shows_its_names_without_more_sql(
    db_session: Session, make_service: ServiceFactory
) -> None:
    user = make_user(db_session)
    make_booking(db_session, user=user)
    (booking,) = own_bookings(make_service(), user.id)
    assert booking.centre.name  # the relationships are `raise_on_sql`, so this proves they loaded
    assert booking.test.name


def test_get_owned_returns_the_booking_with_its_payments_oldest_first(
    db_session: Session, make_service: ServiceFactory
) -> None:
    user = make_user(db_session)
    booking = make_booking(db_session, user=user)
    failed = make_payment(db_session, booking=booking, status=PaymentStatus.FAILED)
    success = make_payment(db_session, booking=booking, status=PaymentStatus.SUCCESS)
    found = make_service().get_owned(user.id, booking.id)
    assert found.id == booking.id
    assert [payment.id for payment in found.payments] == [failed.id, success.id]
    assert found.centre.name
    assert found.test.name


def test_get_owned_of_a_booking_without_payments_has_an_empty_list(
    db_session: Session, make_service: ServiceFactory
) -> None:
    user = make_user(db_session)
    booking = make_booking(db_session, user=user)
    assert make_service().get_owned(user.id, booking.id).payments == []


def test_someone_elses_booking_is_forbidden_and_an_unknown_one_is_not_found(
    db_session: Session, make_service: ServiceFactory
) -> None:
    owner, stranger = make_user(db_session), make_user(db_session)
    booking = make_booking(db_session, user=owner)
    service = make_service()
    with pytest.raises(Forbidden):
        service.get_owned(stranger.id, booking.id)
    with pytest.raises(BookingNotFound):
        service.get_owned(stranger.id, uuid.uuid4())


def test_the_reads_take_two_one_and_two_statements(
    db_engine: Engine, db_session: Session, make_service: ServiceFactory
) -> None:
    owner, stranger = make_user(db_session), make_user(db_session)
    booking = make_booking(db_session, user=owner)
    make_payment(db_session, booking=booking)
    service = make_service()

    def used(action: Callable[[], object]) -> int:
        with QueryCounter(db_engine) as counter, contextlib.suppress(Forbidden):
            action()
        return counter.count

    assert used(lambda: service.list_for_user(owner.id, PageParams())) == 2  # the count, the page
    assert used(lambda: service.get_owned(owner.id, booking.id)) == 1  # booking, names, payments
    assert used(lambda: service.get_owned(stranger.id, booking.id)) == 2  # then the existence read


# ==== cancel (T3.4)


def cancel(service: BookingService, user: object, booking: Booking) -> Booking:
    return service.cancel(user_id=user.id, booking_id=booking.id)  # type: ignore[attr-defined]


def status_of(session: Session, booking: Booking) -> BookingStatus:
    session.expire_all()
    stored = session.scalar(select(Booking.status).where(Booking.id == booking.id))
    assert stored is not None
    return stored


@pytest.mark.parametrize("before", [BookingStatus.PENDING, BookingStatus.CONFIRMED])
def test_a_pending_or_confirmed_booking_can_be_cancelled(
    db_session: Session, make_service: ServiceFactory, before: BookingStatus
) -> None:
    user = make_user(db_session)
    booking = make_booking(db_session, user=user, status=before)
    cancelled = cancel(make_service(), user, booking)
    assert cancelled.status is BookingStatus.CANCELLED
    assert cancelled.centre.name  # loaded for the response
    assert status_of(db_session, booking) is BookingStatus.CANCELLED


def test_a_second_cancel_changes_nothing_and_writes_nothing(
    db_engine: Engine, db_session: Session, make_service: ServiceFactory
) -> None:
    user = make_user(db_session)
    booking = make_booking(db_session, user=user)
    service = make_service()
    cancel(service, user, booking)
    updated_at = db_session.scalar(select(Booking.updated_at).where(Booking.id == booking.id))
    with QueryCounter(db_engine) as counter:
        again = cancel(service, user, booking)
    assert again.status is BookingStatus.CANCELLED
    assert not any(
        statement.lstrip().upper().startswith("UPDATE") for statement in counter.statements
    )
    db_session.expire_all()
    assert (
        db_session.scalar(select(Booking.updated_at).where(Booking.id == booking.id)) == updated_at
    )


def test_a_failed_booking_cannot_be_cancelled(
    db_session: Session, make_service: ServiceFactory
) -> None:
    user = make_user(db_session)
    booking = make_booking(db_session, user=user, status=BookingStatus.FAILED)
    with pytest.raises(InvalidBookingStatus):
        cancel(make_service(), user, booking)
    assert status_of(db_session, booking) is BookingStatus.FAILED


def test_a_booking_with_a_live_pending_payment_cannot_be_cancelled(
    db_session: Session, make_service: ServiceFactory
) -> None:
    user = make_user(db_session)
    booking = make_booking(db_session, user=user)
    make_payment(db_session, booking=booking, status=PaymentStatus.PENDING)
    with pytest.raises(PaymentInProgress):
        cancel(make_service(), user, booking)
    assert status_of(db_session, booking) is BookingStatus.PENDING


def test_a_confirmed_booking_keeps_its_successful_payment_when_it_is_cancelled(
    db_session: Session, make_service: ServiceFactory
) -> None:
    """Assumption A7: a refund is out of scope, so the payment stays as it was."""
    user = make_user(db_session)
    booking = make_booking(db_session, user=user, status=BookingStatus.CONFIRMED)
    payment = make_payment(db_session, booking=booking, status=PaymentStatus.SUCCESS)
    cancel(make_service(), user, booking)
    db_session.expire_all()
    assert db_session.get(type(payment), payment.id).status is PaymentStatus.SUCCESS  # type: ignore[union-attr]


def test_a_failed_payment_does_not_block_a_cancel(
    db_session: Session, make_service: ServiceFactory
) -> None:
    user = make_user(db_session)
    booking = make_booking(db_session, user=user)
    make_payment(db_session, booking=booking, status=PaymentStatus.FAILED)
    assert cancel(make_service(), user, booking).status is BookingStatus.CANCELLED


def test_someone_elses_booking_cannot_be_cancelled_and_stays_as_it_was(
    db_session: Session, make_service: ServiceFactory
) -> None:
    owner, stranger = make_user(db_session), make_user(db_session)
    booking = make_booking(db_session, user=owner)
    with pytest.raises(Forbidden):
        cancel(make_service(), stranger, booking)
    assert status_of(db_session, booking) is BookingStatus.PENDING


def test_an_unknown_booking_cannot_be_cancelled(
    db_session: Session, make_service: ServiceFactory
) -> None:
    with pytest.raises(BookingNotFound):
        make_service().cancel(user_id=make_user(db_session).id, booking_id=uuid.uuid4())


@pytest.mark.parametrize("outcome", ["cancelled", "no-op", "refused", "forbidden", "unknown"])
def test_every_way_out_of_a_cancel_ends_the_transaction_and_frees_the_lock(
    db_engine: Engine, db_session: Session, make_service: ServiceFactory, outcome: str
) -> None:
    """A lock must never outlive the call: FastAPI serialises the response after it returns."""
    user, stranger = make_user(db_session), make_user(db_session)
    status = BookingStatus.FAILED if outcome == "refused" else BookingStatus.PENDING
    booking = make_booking(db_session, user=user, status=status)
    if outcome == "no-op":
        db_session.execute(text("UPDATE bookings SET status = 'CANCELLED'"))
        db_session.commit()
    service = make_service()
    caller = stranger if outcome == "forbidden" else user
    target = uuid.uuid4() if outcome == "unknown" else booking.id
    with contextlib.suppress(Forbidden, BookingNotFound, InvalidBookingStatus):
        service.cancel(user_id=caller.id, booking_id=target)
    with Session(db_engine) as prober:  # NOWAIT fails at once when the row is still locked
        prober.execute(
            select(Booking.id).where(Booking.id == booking.id).with_for_update(nowait=True)
        )
    assert service._session.in_transaction() is False


def test_a_cancel_takes_four_statements(
    db_engine: Engine, db_session: Session, make_service: ServiceFactory
) -> None:
    """The lock, the check for a pending payment, the UPDATE and the names for the response."""
    user = make_user(db_session)
    booking = make_booking(db_session, user=user)
    service = make_service()
    with QueryCounter(db_engine) as counter:
        cancel(service, user, booking)
    assert counter.count == 4, counter.statements

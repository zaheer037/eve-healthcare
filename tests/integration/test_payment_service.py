"""PaymentService.pay and get_owned on a real database (spec sections 7.4 and 8.1).

Covers AB4, AB5, AB20, AB30 and AB31.
"""

import contextlib
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import (
    BookingNotFound,
    BookingNotPayable,
    Forbidden,
    PaymentNotFound,
    SimulationNotAllowed,
)
from app.models.booking import Booking, BookingStatus
from app.models.payment import Payment, PaymentStatus
from app.payments.service import PaymentService
from app.payments.simulator import FAILURE_REASON, PaymentSimulator
from tests.factories import (
    future_slot,
    make_booking,
    make_centre_test,
    make_payment,
    make_user,
)
from tests.support import QueryCounter

FIXED_NOW = datetime(2030, 6, 1, 9, 0, tzinfo=UTC)
ServiceFactory = Callable[..., PaymentService]


@pytest.fixture
def make_service(db_engine: Engine, test_settings: Settings) -> Iterator[ServiceFactory]:
    """`make_service(roll=0.9, payment_success_rate=0.8)` builds a service on its own session."""
    sessions: list[Session] = []

    def build(
        *, roll: float = 0.0, now: datetime | None = None, **changes: object
    ) -> PaymentService:
        session = Session(db_engine, expire_on_commit=False)
        sessions.append(session)
        settings = test_settings.model_copy(update=changes)
        simulator = PaymentSimulator(settings.payment_success_rate, roll=lambda: roll)
        clock = (lambda: now) if now else (lambda: datetime.now(UTC))
        return PaymentService(session, settings, simulator, clock)

    yield build
    for session in sessions:
        session.close()


def count_payments(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(Payment)) or 0


def reload[T](session: Session, model: type[T], key: uuid.UUID) -> T:
    session.expire_all()
    found = session.get(model, key)
    assert found is not None
    return found


# ==== a new payment settles at once


def test_a_successful_payment_confirms_the_booking(
    db_session: Session, make_service: ServiceFactory
) -> None:
    user = make_user(db_session)
    booking = make_booking(
        db_session,
        user=user,
        amount=Decimal("350"),
        appointment_at=FIXED_NOW + timedelta(days=3),
    )
    outcome = make_service(now=FIXED_NOW).pay(
        user_id=user.id, booking_id=booking.id, simulate=PaymentStatus.SUCCESS
    )
    payment = outcome.view.payment
    assert outcome.created is True
    assert outcome.view.booking_status is BookingStatus.CONFIRMED
    assert (payment.status, payment.amount, payment.currency) == (
        PaymentStatus.SUCCESS,
        Decimal("350"),
        "INR",
    )
    assert payment.provider_payment_id == f"sim_{payment.id.hex}"
    assert payment.settled_at == FIXED_NOW
    assert reload(db_session, Booking, booking.id).status is BookingStatus.CONFIRMED
    stored = reload(db_session, Payment, payment.id)
    assert (stored.status, stored.booking_id) == (PaymentStatus.SUCCESS, booking.id)


def test_a_failed_payment_fails_the_booking_for_good(
    db_session: Session, make_service: ServiceFactory
) -> None:
    """Decision D5: a failed payment ends the booking, and the user books again."""
    user = make_user(db_session)
    booking = make_booking(db_session, user=user)
    service = make_service()
    outcome = service.pay(user_id=user.id, booking_id=booking.id, simulate=PaymentStatus.FAILED)
    assert outcome.created is True
    assert outcome.view.payment.status is PaymentStatus.FAILED
    assert outcome.view.payment.failure_reason == FAILURE_REASON
    assert reload(db_session, Booking, booking.id).status is BookingStatus.FAILED
    with pytest.raises(BookingNotPayable):  # a retry after a failure is refused
        service.pay(user_id=user.id, booking_id=booking.id, simulate=None)


def test_pending_mode_leaves_both_rows_pending_for_the_webhook(
    db_session: Session, make_service: ServiceFactory
) -> None:
    user = make_user(db_session)
    booking = make_booking(db_session, user=user)
    outcome = make_service().pay(
        user_id=user.id, booking_id=booking.id, simulate=PaymentStatus.PENDING
    )
    payment = outcome.view.payment
    assert outcome.created is True
    assert (payment.status, payment.settled_at, payment.provider_payment_id) == (
        PaymentStatus.PENDING,
        None,
        None,
    )
    assert reload(db_session, Booking, booking.id).status is BookingStatus.PENDING


@pytest.mark.parametrize(
    ("roll", "expected"), [(0.1, PaymentStatus.SUCCESS), (0.95, PaymentStatus.FAILED)]
)
def test_without_an_override_the_success_rate_decides(
    db_session: Session, make_service: ServiceFactory, roll: float, expected: PaymentStatus
) -> None:
    user = make_user(db_session)
    booking = make_booking(db_session, user=user)
    outcome = make_service(roll=roll, payment_success_rate=0.8).pay(
        user_id=user.id, booking_id=booking.id, simulate=None
    )
    assert outcome.view.payment.status is expected


def test_the_amount_is_the_bookings_and_not_the_current_price(
    db_session: Session, make_service: ServiceFactory
) -> None:
    user = make_user(db_session)
    offering = make_centre_test(db_session, price=Decimal("300"))
    booking = make_booking(db_session, user=user, offering=offering)
    db_session.execute(
        text("UPDATE centre_tests SET price = 900 WHERE centre_id = :c AND test_id = :t"),
        {"c": offering.centre_id, "t": offering.test_id},
    )
    db_session.commit()
    payment = (
        make_service()
        .pay(user_id=user.id, booking_id=booking.id, simulate=PaymentStatus.SUCCESS)
        .view.payment
    )
    assert payment.amount == Decimal("300")


# ==== a replay returns the live payment


@pytest.mark.parametrize("live", [PaymentStatus.SUCCESS, PaymentStatus.PENDING])
def test_a_repeat_returns_the_live_payment_and_creates_nothing(
    db_session: Session, make_service: ServiceFactory, live: PaymentStatus
) -> None:
    user = make_user(db_session)
    booking = make_booking(db_session, user=user)
    service = make_service()
    first = service.pay(user_id=user.id, booking_id=booking.id, simulate=live)
    again = service.pay(user_id=user.id, booking_id=booking.id, simulate=None)
    assert again.created is False
    assert again.view.payment.id == first.view.payment.id
    assert again.view.payment.status is live
    assert count_payments(db_session) == 1


def test_a_cancelled_booking_that_holds_a_successful_payment_replays_it(
    db_session: Session, make_service: ServiceFactory
) -> None:
    """Assumption A7: the cancelled booking keeps its payment, and the replay check comes first."""
    user = make_user(db_session)
    booking = make_booking(db_session, user=user, status=BookingStatus.CANCELLED)
    payment = make_payment(db_session, booking=booking, status=PaymentStatus.SUCCESS)
    outcome = make_service().pay(user_id=user.id, booking_id=booking.id, simulate=None)
    assert outcome.created is False
    assert outcome.view.payment.id == payment.id
    assert outcome.view.booking_status is BookingStatus.CANCELLED


# ==== when a booking cannot be paid


@pytest.mark.parametrize(
    "status", [BookingStatus.FAILED, BookingStatus.CANCELLED, BookingStatus.CONFIRMED]
)
def test_a_booking_that_is_not_pending_and_has_no_live_payment_is_not_payable(
    db_session: Session, make_service: ServiceFactory, status: BookingStatus
) -> None:
    """AB20."""
    user = make_user(db_session)
    booking = make_booking(db_session, user=user, status=status)
    with pytest.raises(BookingNotPayable, match=status.value):
        make_service().pay(user_id=user.id, booking_id=booking.id, simulate=None)
    assert count_payments(db_session) == 0


@pytest.mark.parametrize(
    "started_ago", [timedelta(seconds=0), timedelta(minutes=1), timedelta(days=3)]
)
def test_a_booking_whose_appointment_has_started_is_not_payable(
    db_session: Session, make_service: ServiceFactory, started_ago: timedelta
) -> None:
    """AB31. An appointment that starts exactly now has started."""
    user = make_user(db_session)
    booking = make_booking(db_session, user=user, appointment_at=FIXED_NOW - started_ago)
    with pytest.raises(BookingNotPayable, match="started"):
        make_service(now=FIXED_NOW).pay(user_id=user.id, booking_id=booking.id, simulate=None)
    assert count_payments(db_session) == 0


def test_an_appointment_a_minute_ahead_is_still_payable(
    db_session: Session, make_service: ServiceFactory
) -> None:
    user = make_user(db_session)
    booking = make_booking(db_session, user=user, appointment_at=FIXED_NOW + timedelta(minutes=1))
    outcome = make_service(now=FIXED_NOW).pay(
        user_id=user.id, booking_id=booking.id, simulate=PaymentStatus.SUCCESS
    )
    assert outcome.created is True


# ==== who may pay


def test_someone_elses_booking_is_forbidden_and_gets_no_payment(
    db_session: Session, make_service: ServiceFactory
) -> None:
    """AB4."""
    booking = make_booking(db_session, user=make_user(db_session))
    stranger = make_user(db_session)
    with pytest.raises(Forbidden):
        make_service().pay(
            user_id=stranger.id, booking_id=booking.id, simulate=PaymentStatus.SUCCESS
        )
    assert count_payments(db_session) == 0
    assert reload(db_session, Booking, booking.id).status is BookingStatus.PENDING


def test_an_unknown_booking_is_not_found(db_session: Session, make_service: ServiceFactory) -> None:
    with pytest.raises(BookingNotFound):
        make_service().pay(user_id=make_user(db_session).id, booking_id=uuid.uuid4(), simulate=None)


# ==== the simulation switch


def test_a_forced_result_is_refused_when_the_setting_forbids_it_and_nothing_is_read(
    db_engine: Engine, db_session: Session, make_service: ServiceFactory
) -> None:
    """AB30: production sets PAYMENT_ALLOW_SIMULATE_OVERRIDE to false."""
    user = make_user(db_session)
    booking = make_booking(db_session, user=user)
    service = make_service(payment_allow_simulate_override=False)
    with QueryCounter(db_engine) as counter, pytest.raises(SimulationNotAllowed):
        service.pay(user_id=user.id, booking_id=booking.id, simulate=PaymentStatus.SUCCESS)
    assert counter.count == 0  # the refusal costs no statement
    assert count_payments(db_session) == 0


def test_the_dice_still_work_when_the_override_is_forbidden(
    db_session: Session, make_service: ServiceFactory
) -> None:
    user = make_user(db_session)
    booking = make_booking(db_session, user=user)
    outcome = make_service(payment_allow_simulate_override=False, roll=0.0).pay(
        user_id=user.id, booking_id=booking.id, simulate=None
    )
    assert outcome.view.payment.status is PaymentStatus.SUCCESS


# ==== the transaction and the statements


@pytest.mark.parametrize("path", ["created", "replay", "not-payable", "forbidden", "unknown"])
def test_every_way_out_of_pay_ends_the_transaction_and_frees_the_lock(
    db_engine: Engine, db_session: Session, make_service: ServiceFactory, path: str
) -> None:
    user, stranger = make_user(db_session), make_user(db_session)
    status = BookingStatus.CANCELLED if path == "not-payable" else BookingStatus.PENDING
    booking = make_booking(db_session, user=user, status=status)
    if path == "replay":
        make_payment(db_session, booking=booking, status=PaymentStatus.PENDING)
    service = make_service()
    caller = stranger if path == "forbidden" else user
    target = uuid.uuid4() if path == "unknown" else booking.id
    with contextlib.suppress(Forbidden, BookingNotFound, BookingNotPayable):
        service.pay(user_id=caller.id, booking_id=target, simulate=PaymentStatus.SUCCESS)
    with Session(db_engine) as prober:  # NOWAIT fails at once when the row is still locked
        prober.execute(
            select(Booking.id).where(Booking.id == booking.id).with_for_update(nowait=True)
        )
    assert service._session.in_transaction() is False


def test_the_statement_counts_are_four_three_and_two(
    db_engine: Engine, db_session: Session, make_service: ServiceFactory
) -> None:
    """Create: the lock, the live payment, INSERT payment, UPDATE booking. Pending mode skips the
    UPDATE. A replay stops after the live payment (spec section 7.2)."""
    user = make_user(db_session)
    settled = make_booking(db_session, user=user, appointment_at=future_slot(3))
    pending = make_booking(db_session, user=user, appointment_at=future_slot(4))
    service = make_service()

    def counted(booking: Booking, simulate: PaymentStatus | None) -> int:
        with QueryCounter(db_engine) as counter:
            service.pay(user_id=user.id, booking_id=booking.id, simulate=simulate)
        return counter.count

    assert counted(settled, PaymentStatus.SUCCESS) == 4
    assert counted(pending, PaymentStatus.PENDING) == 3
    assert counted(settled, None) == 2  # a replay of the successful payment
    assert counted(pending, None) == 2  # a replay of the pending payment


# ==== read one payment


def test_an_owner_reads_a_payment_with_the_status_of_its_booking(
    db_session: Session, make_service: ServiceFactory
) -> None:
    user = make_user(db_session)
    booking = make_booking(db_session, user=user, status=BookingStatus.CONFIRMED)
    payment = make_payment(db_session, booking=booking, status=PaymentStatus.SUCCESS)
    view = make_service().get_owned(user.id, payment.id)
    assert view.payment.id == payment.id
    assert view.booking_status is BookingStatus.CONFIRMED


def test_someone_elses_payment_is_forbidden_and_an_unknown_one_is_not_found(
    db_session: Session, make_service: ServiceFactory
) -> None:
    """AB5."""
    payment = make_payment(db_session)
    stranger = make_user(db_session)
    service = make_service()
    with pytest.raises(Forbidden):
        service.get_owned(stranger.id, payment.id)
    with pytest.raises(PaymentNotFound):
        service.get_owned(stranger.id, uuid.uuid4())


def test_reading_a_payment_takes_one_statement_and_a_miss_takes_two(
    db_engine: Engine, db_session: Session, make_service: ServiceFactory
) -> None:
    owner, stranger = make_user(db_session), make_user(db_session)
    payment = make_payment(db_session, booking=make_booking(db_session, user=owner))
    service = make_service()
    with QueryCounter(db_engine) as counter:
        service.get_owned(owner.id, payment.id)
    assert counter.count == 1
    with QueryCounter(db_engine) as counter, pytest.raises(Forbidden):
        service.get_owned(stranger.id, payment.id)
    assert counter.count == 2

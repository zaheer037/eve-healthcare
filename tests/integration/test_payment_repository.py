"""Payment queries on a real database: what each returns, the locks it takes, and the index it
uses (architecture §3.4)."""

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import NoResultFound, OperationalError
from sqlalchemy.orm import Session

from app.bookings.repository import BookingRepository
from app.models.booking import Booking, BookingStatus
from app.models.payment import Payment, PaymentStatus
from app.payments.repository import PaymentRepository
from tests.factories import make_booking, make_payment, make_user
from tests.support import indexes_used


@pytest.fixture
def session(db_engine: Engine) -> Iterator[Session]:
    with Session(db_engine, expire_on_commit=False) as own:
        yield own


# ==== the live payment of a booking


def test_a_pending_or_successful_payment_is_the_live_one(
    db_session: Session, session: Session
) -> None:
    for status in (PaymentStatus.PENDING, PaymentStatus.SUCCESS):
        booking = make_booking(db_session)
        payment = make_payment(db_session, booking=booking, status=status)
        live = PaymentRepository(session).live_payment(booking.id)
        assert live is not None
        assert live.id == payment.id


def test_a_failed_payment_is_not_live_and_a_booking_without_payments_has_none(
    db_session: Session, session: Session
) -> None:
    booking = make_booking(db_session)
    make_payment(db_session, booking=booking, status=PaymentStatus.FAILED)
    repository = PaymentRepository(session)
    assert repository.live_payment(booking.id) is None
    assert repository.live_payment(make_booking(db_session).id) is None


def test_the_live_payment_is_the_one_that_follows_a_failure(
    db_session: Session, session: Session
) -> None:
    booking = make_booking(db_session)
    make_payment(db_session, booking=booking, status=PaymentStatus.FAILED)
    pending = make_payment(db_session, booking=booking, status=PaymentStatus.PENDING)
    live = PaymentRepository(session).live_payment(booking.id)
    assert live is not None
    assert live.id == pending.id


# ==== the booking of a payment


def test_the_booking_of_a_known_payment_is_found_and_an_unknown_payment_has_none(
    db_session: Session, session: Session
) -> None:
    payment = make_payment(db_session)
    repository = PaymentRepository(session)
    assert repository.booking_id_of(payment.id) == payment.booking_id
    assert repository.booking_id_of(uuid.uuid4()) is None


# ==== the row lock


def test_lock_returns_the_payment_and_an_unknown_id_is_a_bug(
    db_session: Session, session: Session
) -> None:
    """The webhook locks a payment only after it found its booking, and payments are never
    deleted. A missing row is not a business case, so it raises."""
    payment = make_payment(db_session)
    repository = PaymentRepository(session)
    assert repository.lock(payment.id).id == payment.id
    with pytest.raises(NoResultFound):
        repository.lock(uuid.uuid4())


def test_lock_holds_the_row_until_the_transaction_ends(
    db_session: Session, session: Session, db_engine: Engine
) -> None:
    payment = make_payment(db_session)
    PaymentRepository(session).lock(payment.id)
    with Session(db_engine) as rival:
        with pytest.raises(OperationalError):  # NOWAIT: the row is locked at once
            rival.execute(
                select(Payment.id).where(Payment.id == payment.id).with_for_update(nowait=True)
            )
        rival.rollback()
        session.rollback()  # the lock goes away with the transaction
        rival.execute(
            select(Payment.id).where(Payment.id == payment.id).with_for_update(nowait=True)
        )


def test_lock_takes_no_key_update_so_a_child_insert_is_not_blocked(
    db_session: Session, session: Session, db_engine: Engine
) -> None:
    """A child row takes FOR KEY SHARE on the payment. FOR UPDATE would conflict with it, and two
    webhooks for one payment would deadlock. FOR NO KEY UPDATE does not."""
    payment = make_payment(db_session)
    PaymentRepository(session).lock(payment.id)
    with Session(db_engine) as child:
        child.execute(text("SET LOCAL lock_timeout = '500ms'"))
        child.execute(  # the foreign key check takes FOR KEY SHARE on the payment
            text(
                "INSERT INTO webhook_events (id, provider, event_id, payload, payment_id)"
                " VALUES (gen_random_uuid(), 'SIMULATOR', 'evt_child', '{}', :p)"
            ),
            {"p": payment.id},
        )
        child.rollback()


def test_lock_refreshes_an_object_that_the_session_already_holds(
    db_session: Session, session: Session
) -> None:
    """Without populate_existing the ORM would hand back the stale object."""
    payment = make_payment(db_session)
    repository = PaymentRepository(session)
    held = repository.lock(payment.id)
    assert held.status is PaymentStatus.PENDING
    session.rollback()
    db_session.execute(
        text("UPDATE payments SET status = 'FAILED', settled_at = now() WHERE id = :id"),
        {"id": payment.id},
    )
    db_session.commit()
    again = repository.lock(payment.id)
    assert again is held  # the same object in the identity map
    assert again.status is PaymentStatus.FAILED  # with the values that the lock just read


# ==== the provider reference


def test_a_reference_held_by_another_payment_is_a_conflict(
    db_session: Session, session: Session
) -> None:
    holder = make_payment(db_session, provider_payment_id="sim_1")
    other = make_payment(db_session)
    repository = PaymentRepository(session)
    assert repository.reference_held_by_another(other, "sim_1") is True
    assert repository.reference_held_by_another(holder, "sim_1") is False  # its own reference
    assert repository.reference_held_by_another(other, "sim_2") is False  # nobody holds it


def test_the_same_reference_at_another_provider_is_no_conflict(
    db_session: Session, session: Session
) -> None:
    make_payment(db_session, provider="OTHER", provider_payment_id="sim_1")
    mine = make_payment(db_session)  # provider SIMULATOR
    assert PaymentRepository(session).reference_held_by_another(mine, "sim_1") is False


# ==== an owner reads a payment


def test_an_owner_reads_a_payment_with_the_status_of_its_booking(
    db_session: Session, session: Session
) -> None:
    owner = make_user(db_session)
    booking = make_booking(db_session, user=owner, status=BookingStatus.CONFIRMED)
    payment = make_payment(db_session, booking=booking, status=PaymentStatus.SUCCESS)
    found = PaymentRepository(session).get_owned(payment.id, owner.id)
    assert found is not None
    read, booking_status = found
    assert read.id == payment.id
    assert booking_status is BookingStatus.CONFIRMED


def test_a_stranger_and_an_unknown_id_find_nothing_but_the_existence_read_tells_them_apart(
    db_session: Session, session: Session
) -> None:
    payment = make_payment(db_session)
    stranger = make_user(db_session)
    repository = PaymentRepository(session)
    assert repository.get_owned(payment.id, stranger.id) is None
    assert repository.get_owned(uuid.uuid4(), stranger.id) is None
    assert repository.exists(payment.id) is True
    assert repository.exists(uuid.uuid4()) is False


# ==== the booking lock that the webhook takes without an owner


def test_the_webhook_locks_a_booking_by_its_id(db_session: Session, session: Session) -> None:
    booking = make_booking(db_session)
    locked = BookingRepository(session).lock(booking.id)
    assert isinstance(locked, Booking)
    assert locked.id == booking.id


# ==== every query uses the index that architecture §3.4 names


def test_each_query_uses_its_index(db_engine: Engine, db_session: Session) -> None:
    owner = make_user(db_session)
    booking = make_booking(db_session, user=owner)
    payment = make_payment(db_session, booking=booking, provider_payment_id="sim_x")

    def run(query: str) -> set[str]:
        def action() -> None:
            with Session(db_engine) as own:
                repository = PaymentRepository(own)
                if query == "live":
                    repository.live_payment(booking.id)
                elif query == "booking_id_of":
                    repository.booking_id_of(payment.id)
                elif query == "lock":
                    repository.lock(payment.id)
                elif query == "reference":
                    repository.reference_held_by_another(payment, "sim_y")
                elif query == "owned":
                    repository.get_owned(payment.id, owner.id)
                elif query == "exists":
                    repository.exists(payment.id)
                elif query == "lock_booking":
                    BookingRepository(own).lock(booking.id)
                own.rollback()

        return indexes_used(db_engine, action)

    assert "uq_payments_live_per_booking" in run("live")
    assert "pk_payments" in run("booking_id_of")
    assert "pk_payments" in run("lock")
    assert "uq_payments_provider_ref" in run("reference")
    assert "pk_payments" in run("owned")  # the booking side may use any of its indexes
    assert "pk_payments" in run("exists")
    assert "pk_bookings" in run("lock_booking")

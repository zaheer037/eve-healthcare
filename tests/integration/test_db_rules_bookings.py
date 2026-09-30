"""Each rule of architecture §3.5 for bookings, payments and webhook events: a bad row is refused
and a good one is accepted."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.orm import Session

from app.core.db import violated_constraint
from app.models.booking import Booking, BookingStatus
from app.models.payment import Payment, PaymentStatus, WebhookEvent, WebhookOutcome
from tests.factories import (
    future_slot,
    make_booking,
    make_centre,
    make_centre_test,
    make_diagnostic_test,
    make_payment,
    make_user,
    make_webhook_event,
)


def refused_by(session: Session) -> str | None:
    """Flush and return the name of the constraint that refused the pending row."""
    with pytest.raises(IntegrityError) as error:
        session.flush()
    session.rollback()
    return violated_constraint(error.value)


def new_booking(
    session: Session, status: BookingStatus = BookingStatus.PENDING, **changes: object
) -> Booking:
    """An unsaved booking for a fresh user and offering. `changes` replaces any column."""
    offering = make_centre_test(session)
    columns: dict[str, object] = {
        "user_id": make_user(session).id,
        "centre_id": offering.centre_id,
        "test_id": offering.test_id,
        "appointment_at": future_slot(),
        "amount": Decimal("499"),
        "currency": "INR",
        "status": status,
    }
    return Booking(**{**columns, **changes})


# ---------------------------------------------------------------- bookings


def test_a_booking_gets_its_defaults_and_keeps_the_price_that_it_copied(
    db_session: Session,
) -> None:
    booking = make_booking(db_session, amount=Decimal("350"))
    db_session.refresh(booking)
    assert booking.status is BookingStatus.PENDING
    assert booking.amount == Decimal("350.00")
    assert booking.created_at is not None


def test_the_database_defaults_a_missing_booking_status_to_pending(db_session: Session) -> None:
    offering = make_centre_test(db_session)
    user = make_user(db_session)
    status = db_session.scalar(
        text(
            "INSERT INTO bookings (id, user_id, centre_id, test_id, appointment_at, amount,"
            " currency) VALUES (gen_random_uuid(), :u, :c, :t, now(), 10, 'INR') RETURNING status"
        ),
        {"u": user.id, "c": offering.centre_id, "t": offering.test_id},
    )
    assert status == "PENDING"


@pytest.mark.parametrize("amount", ["0.01", "99999999.99"])
def test_the_booking_amount_bounds_are_accepted(db_session: Session, amount: str) -> None:
    db_session.add(new_booking(db_session, amount=Decimal(amount)))
    db_session.flush()


@pytest.mark.parametrize("amount", ["0", "0.004", "-1", "NaN"])
def test_a_booking_amount_outside_the_bounds_is_refused(db_session: Session, amount: str) -> None:
    db_session.add(new_booking(db_session, amount=Decimal(amount)))
    assert refused_by(db_session) == "ck_bookings_amount"


@pytest.mark.parametrize("currency", ["inr", "INRR", "IN", ""])
def test_a_booking_currency_must_be_three_upper_case_letters(
    db_session: Session, currency: str
) -> None:
    db_session.add(new_booking(db_session, currency=currency))
    assert refused_by(db_session) == "ck_bookings_currency"


def test_a_booking_status_must_be_one_of_the_four(db_session: Session) -> None:
    """The ORM refuses an unknown member before the database does, so this uses raw SQL."""
    offering = make_centre_test(db_session)
    user = make_user(db_session)
    with pytest.raises(IntegrityError) as error:
        db_session.execute(
            text(
                "INSERT INTO bookings (id, user_id, centre_id, test_id, appointment_at, amount,"
                " currency, status) VALUES (gen_random_uuid(), :u, :c, :t, now(), 10, 'INR',"
                " 'REFUNDED')"
            ),
            {"u": user.id, "c": offering.centre_id, "t": offering.test_id},
        )
    assert violated_constraint(error.value) == "ck_bookings_status"


def test_a_booking_needs_a_known_user(db_session: Session) -> None:
    db_session.add(new_booking(db_session, user_id=uuid.uuid4()))
    assert refused_by(db_session) == "fk_bookings_user_id"


def test_a_booking_needs_a_test_that_the_centre_offers(db_session: Session) -> None:
    """The composite foreign key to `centre_tests`: both the centre and the test exist, but the
    centre does not offer this test."""
    centre, other_test = make_centre(db_session), make_diagnostic_test(db_session)
    db_session.add(new_booking(db_session, centre_id=centre.id, test_id=other_test.id))
    assert refused_by(db_session) == "fk_bookings_centre_test"


# ---- the active slot


def test_a_user_cannot_hold_two_active_bookings_for_one_slot(db_session: Session) -> None:
    first = make_booking(db_session)
    db_session.add(
        new_booking(
            db_session,
            user_id=first.user_id,
            centre_id=first.centre_id,
            test_id=first.test_id,
            appointment_at=first.appointment_at,
        )
    )
    assert refused_by(db_session) == "uq_bookings_active_slot"


@pytest.mark.parametrize("held", [BookingStatus.PENDING, BookingStatus.CONFIRMED])
def test_pending_and_confirmed_bookings_hold_the_slot(
    db_session: Session, held: BookingStatus
) -> None:
    first = make_booking(db_session, status=held)
    db_session.add(
        new_booking(
            db_session,
            user_id=first.user_id,
            centre_id=first.centre_id,
            test_id=first.test_id,
            appointment_at=first.appointment_at,
        )
    )
    assert refused_by(db_session) == "uq_bookings_active_slot"


@pytest.mark.parametrize("released", [BookingStatus.CANCELLED, BookingStatus.FAILED])
def test_a_cancelled_or_failed_booking_frees_the_slot(
    db_session: Session, released: BookingStatus
) -> None:
    first = make_booking(db_session, status=released)
    db_session.add(
        new_booking(
            db_session,
            user_id=first.user_id,
            centre_id=first.centre_id,
            test_id=first.test_id,
            appointment_at=first.appointment_at,
        )
    )
    db_session.flush()  # accepted


def test_another_time_or_another_user_is_not_a_duplicate(db_session: Session) -> None:
    first = make_booking(db_session)
    same_slot = {
        "centre_id": first.centre_id,
        "test_id": first.test_id,
        "appointment_at": first.appointment_at,
    }
    db_session.add(new_booking(db_session, **same_slot))  # another user
    db_session.add(  # the same user, another minute
        new_booking(
            db_session,
            **{**same_slot, "user_id": first.user_id, "appointment_at": future_slot(days=8)},
        )
    )
    db_session.flush()


# ---- history stays


def test_a_user_with_bookings_cannot_be_deleted(db_session: Session) -> None:
    booking = make_booking(db_session)
    with pytest.raises(IntegrityError) as error:
        db_session.execute(text("DELETE FROM users WHERE id = :id"), {"id": booking.user_id})
    assert violated_constraint(error.value) == "fk_bookings_user_id"


def test_a_price_with_bookings_cannot_be_deleted(db_session: Session) -> None:
    booking = make_booking(db_session)
    with pytest.raises(IntegrityError) as error:
        db_session.execute(
            text("DELETE FROM centre_tests WHERE centre_id = :c AND test_id = :t"),
            {"c": booking.centre_id, "t": booking.test_id},
        )
    assert violated_constraint(error.value) == "fk_bookings_centre_test"


# ---------------------------------------------------------------- payments


def new_payment(session: Session, **changes: object) -> Payment:
    booking = make_booking(session)
    columns: dict[str, object] = {
        "booking_id": booking.id,
        "amount": booking.amount,
        "currency": booking.currency,
        "status": PaymentStatus.PENDING,
    }
    return Payment(**{**columns, **changes})


def test_a_payment_gets_its_defaults(db_session: Session) -> None:
    payment = make_payment(db_session)
    db_session.refresh(payment)
    assert (payment.provider, payment.status) == ("SIMULATOR", PaymentStatus.PENDING)
    assert (payment.provider_payment_id, payment.failure_reason, payment.settled_at) == (
        None,
        None,
        None,
    )


@pytest.mark.parametrize("amount", ["0", "0.004", "-1", "NaN"])
def test_a_payment_amount_outside_the_bounds_is_refused(db_session: Session, amount: str) -> None:
    db_session.add(new_payment(db_session, amount=Decimal(amount)))
    assert refused_by(db_session) == "ck_payments_amount"


@pytest.mark.parametrize("currency", ["inr", "INRR", ""])
def test_a_payment_currency_must_be_three_upper_case_letters(
    db_session: Session, currency: str
) -> None:
    db_session.add(new_payment(db_session, currency=currency))
    assert refused_by(db_session) == "ck_payments_currency"


def test_a_payment_status_must_be_one_of_the_three(db_session: Session) -> None:
    booking = make_booking(db_session)
    with pytest.raises(IntegrityError) as error:
        db_session.execute(
            text(
                "INSERT INTO payments (id, booking_id, amount, currency, status, settled_at)"
                " VALUES (gen_random_uuid(), :b, 10, 'INR', 'REFUNDED', now())"
            ),
            {"b": booking.id},
        )
    assert violated_constraint(error.value) == "ck_payments_status"


def test_a_payment_needs_a_known_booking(db_session: Session) -> None:
    db_session.add(new_payment(db_session, booking_id=uuid.uuid4()))
    assert refused_by(db_session) == "fk_payments_booking_id"


def test_a_booking_with_payments_cannot_be_deleted(db_session: Session) -> None:
    payment = make_payment(db_session)
    with pytest.raises(IntegrityError) as error:
        db_session.execute(text("DELETE FROM bookings WHERE id = :id"), {"id": payment.booking_id})
    assert violated_constraint(error.value) == "fk_payments_booking_id"


# ---- settled_at goes with the status


@pytest.mark.parametrize("status", [PaymentStatus.SUCCESS, PaymentStatus.FAILED])
def test_a_settled_payment_needs_a_settlement_time(
    db_session: Session, status: PaymentStatus
) -> None:
    db_session.add(new_payment(db_session, status=status, settled_at=None))
    assert refused_by(db_session) == "ck_payments_settled"


def test_a_pending_payment_has_no_settlement_time(db_session: Session) -> None:
    db_session.add(new_payment(db_session, settled_at=datetime.now(UTC)))
    assert refused_by(db_session) == "ck_payments_settled"


@pytest.mark.parametrize("status", list(PaymentStatus))
def test_a_payment_with_a_consistent_settlement_time_is_accepted(
    db_session: Session, status: PaymentStatus
) -> None:
    assert make_payment(db_session, status=status).status is status


# ---- at most one live payment per booking


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (PaymentStatus.PENDING, PaymentStatus.PENDING),
        (PaymentStatus.PENDING, PaymentStatus.SUCCESS),
        (PaymentStatus.SUCCESS, PaymentStatus.PENDING),
        (PaymentStatus.SUCCESS, PaymentStatus.SUCCESS),
    ],
)
def test_a_booking_has_at_most_one_live_payment(
    db_session: Session, first: PaymentStatus, second: PaymentStatus
) -> None:
    booking = make_booking(db_session)
    make_payment(db_session, booking=booking, status=first)
    db_session.add(
        Payment(
            booking_id=booking.id,
            amount=booking.amount,
            currency=booking.currency,
            status=second,
            settled_at=None if second is PaymentStatus.PENDING else datetime.now(UTC),
        )
    )
    assert refused_by(db_session) == "uq_payments_live_per_booking"


def test_a_failed_payment_is_over_so_another_can_follow_it(db_session: Session) -> None:
    booking = make_booking(db_session)
    make_payment(db_session, booking=booking, status=PaymentStatus.FAILED)
    make_payment(db_session, booking=booking, status=PaymentStatus.FAILED)
    make_payment(db_session, booking=booking, status=PaymentStatus.PENDING)


# ---- provider references


def test_an_empty_provider_reference_is_refused_and_a_missing_one_is_fine(
    db_session: Session,
) -> None:
    db_session.add(new_payment(db_session, provider_payment_id=""))
    assert refused_by(db_session) == "ck_payments_provider_ref"
    assert make_payment(db_session).provider_payment_id is None


def test_one_provider_reference_names_one_payment(db_session: Session) -> None:
    make_payment(db_session, provider_payment_id="pay_1")
    db_session.add(new_payment(db_session, provider_payment_id="pay_1"))
    assert refused_by(db_session) == "uq_payments_provider_ref"


def test_the_same_reference_at_another_provider_or_no_reference_twice_is_fine(
    db_session: Session,
) -> None:
    make_payment(db_session, provider_payment_id="pay_1")
    make_payment(db_session, provider="OTHER", provider_payment_id="pay_1")
    make_payment(db_session)
    make_payment(db_session)


# ---------------------------------------------------------------- webhook events


def test_an_event_in_flight_has_neither_outcome_nor_processing_time(db_session: Session) -> None:
    event = make_webhook_event(db_session)
    assert (event.outcome, event.processed_at) == (None, None)
    assert event.received_at is not None


@pytest.mark.parametrize("outcome", list(WebhookOutcome))
def test_a_processed_event_has_an_outcome_and_a_processing_time(
    db_session: Session, outcome: WebhookOutcome
) -> None:
    assert make_webhook_event(db_session, outcome=outcome).processed_at is not None


def test_an_outcome_without_a_processing_time_is_refused(db_session: Session) -> None:
    db_session.add(
        WebhookEvent(
            provider="SIMULATOR", event_id="e1", payload={}, outcome=WebhookOutcome.APPLIED
        )
    )
    assert refused_by(db_session) == "ck_webhook_events_processed"


def test_a_processing_time_without_an_outcome_is_refused(db_session: Session) -> None:
    db_session.add(
        WebhookEvent(
            provider="SIMULATOR", event_id="e1", payload={}, processed_at=datetime.now(UTC)
        )
    )
    assert refused_by(db_session) == "ck_webhook_events_processed"


def test_an_outcome_must_be_one_of_the_four(db_session: Session) -> None:
    with pytest.raises(IntegrityError) as error:
        db_session.execute(
            text(
                "INSERT INTO webhook_events"
                " (id, provider, event_id, payload, outcome, processed_at)"
                " VALUES (gen_random_uuid(), 'SIMULATOR', 'e1', '{}', 'SHRUGGED', now())"
            )
        )
    assert violated_constraint(error.value) == "ck_webhook_events_outcome"


def test_an_event_is_stored_once_per_provider_and_event_id(db_session: Session) -> None:
    make_webhook_event(db_session, event_id="evt_1")
    make_webhook_event(db_session, provider="OTHER", event_id="evt_1")  # another provider is fine
    db_session.add(WebhookEvent(provider="SIMULATOR", event_id="evt_1", payload={}))
    assert refused_by(db_session) == "uq_webhook_events_provider_event"


def test_an_event_can_name_no_payment_but_not_an_unknown_one(db_session: Session) -> None:
    assert make_webhook_event(db_session).payment_id is None  # an event for an unknown payment
    db_session.add(
        WebhookEvent(provider="SIMULATOR", event_id="e2", payload={}, payment_id=uuid.uuid4())
    )
    assert refused_by(db_session) == "fk_webhook_events_payment_id"


def test_a_payment_with_events_cannot_be_deleted(db_session: Session) -> None:
    payment = make_payment(db_session)
    make_webhook_event(db_session, payment=payment)
    with pytest.raises(IntegrityError) as error:
        db_session.execute(text("DELETE FROM payments WHERE id = :id"), {"id": payment.id})
    assert violated_constraint(error.value) == "fk_webhook_events_payment_id"


def test_the_detail_code_is_short(db_session: Session) -> None:
    db_session.add(WebhookEvent(provider="P", event_id="e", payload={}, detail="x" * 65))
    with pytest.raises(DataError):
        db_session.flush()

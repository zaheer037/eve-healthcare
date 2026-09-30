"""settle(): every row of the decision table in spec section 6.3. No database is involved."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

import pytest

from app.models.booking import Booking, BookingStatus
from app.models.payment import Payment, PaymentStatus, WebhookOutcome
from app.payments.settlement import PaymentResult, Settlement, event_mismatch, settle

NOW = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
PENDING, SUCCESS, FAILED = PaymentStatus.PENDING, PaymentStatus.SUCCESS, PaymentStatus.FAILED
B_PENDING, CONFIRMED, B_FAILED, CANCELLED = (
    BookingStatus.PENDING,
    BookingStatus.CONFIRMED,
    BookingStatus.FAILED,
    BookingStatus.CANCELLED,
)
APPLIED, NOOP, REVIEW = (
    WebhookOutcome.APPLIED,
    WebhookOutcome.IGNORED_NOOP,
    WebhookOutcome.NEEDS_REVIEW,
)


def entities(
    payment: PaymentStatus, booking: BookingStatus, stored_reference: str | None = None
) -> tuple[Booking, Payment]:
    return (
        Booking(status=booking),
        Payment(status=payment, provider_payment_id=stored_reference),
    )


def result(
    status: Literal[PaymentStatus.SUCCESS, PaymentStatus.FAILED],
    reference: str | None = None,
    reason: str | None = None,
) -> PaymentResult:
    return PaymentResult(status=status, provider_payment_id=reference, failure_reason=reason)


# ==== the decision table, one row at a time


def test_a_pending_payment_that_succeeds_confirms_the_booking() -> None:
    booking, payment = entities(PENDING, B_PENDING)
    outcome = settle(booking, payment, result(SUCCESS, "ref_1"), now=NOW)
    assert outcome == Settlement(APPLIED)
    assert (payment.status, payment.provider_payment_id, payment.settled_at) == (
        SUCCESS,
        "ref_1",
        NOW,
    )
    assert payment.failure_reason is None
    assert booking.status is CONFIRMED


def test_a_pending_payment_that_fails_fails_the_booking_and_keeps_the_reason() -> None:
    booking, payment = entities(PENDING, B_PENDING)
    outcome = settle(booking, payment, result(FAILED, "ref_1", "card_declined"), now=NOW)
    assert outcome == Settlement(APPLIED)
    assert (payment.status, payment.failure_reason, payment.settled_at) == (
        FAILED,
        "card_declined",
        NOW,
    )
    assert booking.status is B_FAILED


@pytest.mark.parametrize("booking_status", [CONFIRMED, B_FAILED, CANCELLED])
def test_a_failure_for_a_booking_that_is_not_pending_settles_the_payment_only(
    booking_status: BookingStatus,
) -> None:
    """Unreachable while the cancel guard holds. The table still defines it."""
    booking, payment = entities(PENDING, booking_status)
    assert settle(booking, payment, result(FAILED), now=NOW) == Settlement(APPLIED)
    assert payment.status is FAILED
    assert booking.status is booking_status


@pytest.mark.parametrize("booking_status", [CONFIRMED, B_FAILED, CANCELLED])
def test_a_success_for_a_booking_that_is_not_pending_needs_review_but_records_the_payment(
    booking_status: BookingStatus,
) -> None:
    booking, payment = entities(PENDING, booking_status)
    outcome = settle(booking, payment, result(SUCCESS, "ref_1"), now=NOW)
    assert outcome == Settlement(REVIEW, "booking_not_pending")
    assert (payment.status, payment.provider_payment_id, payment.settled_at) == (
        SUCCESS,
        "ref_1",
        NOW,
    )
    assert booking.status is booking_status  # never touched


@pytest.mark.parametrize("status", [SUCCESS, FAILED])
@pytest.mark.parametrize("booking_status", list(BookingStatus))
@pytest.mark.parametrize("incoming_reference", [None, "ref_1"])
def test_the_same_result_again_with_a_matching_reference_is_a_no_op(
    status: Literal[PaymentStatus.SUCCESS, PaymentStatus.FAILED],
    booking_status: BookingStatus,
    incoming_reference: str | None,
) -> None:
    booking, payment = entities(status, booking_status, stored_reference="ref_1")
    assert settle(booking, payment, result(status, incoming_reference), now=NOW) == Settlement(NOOP)
    assert payment.settled_at is None  # nothing was written
    assert (payment.status, payment.provider_payment_id) == (status, "ref_1")
    assert booking.status is booking_status


@pytest.mark.parametrize("status", [SUCCESS, FAILED])
def test_the_same_result_with_no_stored_reference_and_a_new_one_stores_the_reference(
    status: Literal[PaymentStatus.SUCCESS, PaymentStatus.FAILED],
) -> None:
    booking, payment = entities(status, CONFIRMED, stored_reference=None)
    assert settle(booking, payment, result(status, "ref_1"), now=NOW) == Settlement(APPLIED)
    assert payment.provider_payment_id == "ref_1"
    assert payment.settled_at is None  # the payment was settled long ago


@pytest.mark.parametrize("status", [SUCCESS, FAILED])
def test_the_same_result_with_another_reference_needs_review_and_changes_nothing(
    status: Literal[PaymentStatus.SUCCESS, PaymentStatus.FAILED],
) -> None:
    booking, payment = entities(status, CONFIRMED, stored_reference="ref_1")
    outcome = settle(booking, payment, result(status, "ref_2"), now=NOW)
    assert outcome == Settlement(REVIEW, "reference_mismatch")
    assert payment.provider_payment_id == "ref_1"


@pytest.mark.parametrize(("stored", "incoming"), [(SUCCESS, FAILED), (FAILED, SUCCESS)])
@pytest.mark.parametrize("booking_status", list(BookingStatus))
def test_a_contradicting_terminal_result_needs_review_and_changes_nothing(
    stored: PaymentStatus,
    incoming: Literal[PaymentStatus.SUCCESS, PaymentStatus.FAILED],
    booking_status: BookingStatus,
) -> None:
    booking, payment = entities(stored, booking_status, stored_reference="ref_1")
    outcome = settle(booking, payment, result(incoming, "ref_1"), now=NOW)
    assert outcome == Settlement(REVIEW, "terminal_conflict")
    assert (payment.status, payment.provider_payment_id, payment.settled_at) == (
        stored,
        "ref_1",
        None,
    )
    assert booking.status is booking_status


# ==== the value types


def test_a_settlement_is_built_by_its_named_constructors() -> None:
    assert Settlement.applied() == Settlement(APPLIED, None)
    assert Settlement.noop() == Settlement(NOOP, None)
    assert Settlement.review("terminal_conflict") == Settlement(REVIEW, "terminal_conflict")


def test_a_review_is_the_only_outcome_that_asks_for_attention() -> None:
    assert Settlement.review("x").needs_attention is True
    assert Settlement.applied().needs_attention is False
    assert Settlement.noop().needs_attention is False


# ==== the checks that come before settle(): does the event describe this payment?


def mismatch(amount: str | None, currency: str | None) -> str | None:
    return event_mismatch(
        stored_amount=Decimal("499.00"),
        stored_currency="INR",
        amount=Decimal(amount) if amount is not None else None,
        currency=currency,
    )


@pytest.mark.parametrize(
    ("amount", "currency"),
    [(None, None), ("499.00", "INR"), ("499", "INR"), ("499.0", None), (None, "INR")],
)
def test_an_event_that_agrees_with_the_payment_or_says_nothing_matches(
    amount: str | None, currency: str | None
) -> None:
    assert mismatch(amount, currency) is None


@pytest.mark.parametrize("amount", ["498.99", "499.01", "1.00", "0.01", "99999999.99"])
def test_another_amount_is_a_mismatch(amount: str) -> None:
    assert mismatch(amount, "INR") == "amount_mismatch"


@pytest.mark.parametrize("currency", ["USD", "inr", "EUR"])
def test_another_currency_is_a_mismatch(currency: str) -> None:
    assert mismatch("499.00", currency) == "currency_mismatch"
    assert mismatch(None, currency) == "currency_mismatch"


def test_the_amount_is_judged_before_the_currency() -> None:
    assert mismatch("1.00", "USD") == "amount_mismatch"

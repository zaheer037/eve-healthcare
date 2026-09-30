"""PaymentService.handle_webhook on a real database (architecture §§4.3, 6.2 and 6.5).

Covers AB10 to AB13, AB23 and AB24 at the service level. The HTTP layer has its own tests.
"""

import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from structlog.testing import capture_logs

from app.core.config import Settings
from app.core.errors import ServiceUnavailable
from app.models.booking import Booking, BookingStatus
from app.models.payment import Payment, PaymentStatus, WebhookEvent, WebhookOutcome
from app.payments.schemas import WebhookAck, WebhookPayload
from app.payments.service import PaymentService
from app.payments.simulator import PaymentSimulator
from tests.factories import make_booking, make_payment
from tests.support import QueryCounter

NOW = datetime(2030, 6, 1, 9, 0, tzinfo=UTC)
ServiceFactory = Callable[[], PaymentService]


@pytest.fixture
def make_service(db_engine: Engine, test_settings: Settings) -> Iterator[ServiceFactory]:
    sessions: list[Session] = []

    def build() -> PaymentService:
        session = Session(db_engine, expire_on_commit=False)
        sessions.append(session)
        simulator = PaymentSimulator(test_settings.payment_success_rate)
        return PaymentService(session, test_settings, simulator, lambda: NOW)

    yield build
    for session in sessions:
        session.close()


def event(payment_id: uuid.UUID, **fields: Any) -> WebhookPayload:
    body = {"event_id": f"evt_{uuid.uuid4().hex[:10]}", "payment_id": str(payment_id), **fields}
    return WebhookPayload.model_validate({"status": "SUCCESS", **body})


def pending(db_session: Session) -> Payment:
    return make_payment(db_session, booking=make_booking(db_session))


def processed(outcome: WebhookOutcome, detail: str | None = None) -> WebhookAck:
    return WebhookAck(status="processed", outcome=outcome, detail=detail)


def state(db_session: Session, payment: Payment) -> tuple[PaymentStatus, BookingStatus]:
    db_session.expire_all()
    stored = db_session.get(Payment, payment.id)
    booking = db_session.get(Booking, payment.booking_id)
    assert stored is not None
    assert booking is not None
    return stored.status, booking.status


def events(db_session: Session) -> list[WebhookEvent]:
    db_session.expire_all()
    return list(db_session.scalars(select(WebhookEvent).order_by(WebhookEvent.received_at)))


# ==== a valid event settles the payment


def test_a_success_event_settles_a_pending_payment_and_confirms_its_booking(
    db_session: Session, make_service: ServiceFactory
) -> None:
    payment = pending(db_session)
    ack = make_service().handle_webhook(
        event(payment.id, provider_payment_id="sim_1", amount="499.00", currency="INR")
    )
    assert ack == processed(WebhookOutcome.APPLIED)
    assert state(db_session, payment) == (PaymentStatus.SUCCESS, BookingStatus.CONFIRMED)
    stored = db_session.get(Payment, payment.id)
    assert stored is not None
    assert (stored.provider_payment_id, stored.settled_at) == ("sim_1", NOW)


def test_a_failed_event_fails_the_payment_and_the_booking_and_keeps_the_reason(
    db_session: Session, make_service: ServiceFactory
) -> None:
    payment = pending(db_session)
    ack = make_service().handle_webhook(
        event(payment.id, status="FAILED", failure_reason="card_declined")
    )
    assert ack == processed(WebhookOutcome.APPLIED)
    assert state(db_session, payment) == (PaymentStatus.FAILED, BookingStatus.FAILED)
    stored = db_session.get(Payment, payment.id)
    assert stored is not None
    assert stored.failure_reason == "card_declined"


def test_the_event_row_ends_with_its_outcome_its_payment_and_its_time(
    db_session: Session, make_service: ServiceFactory
) -> None:
    payment = pending(db_session)
    incoming = event(payment.id)
    make_service().handle_webhook(incoming)
    (stored,) = events(db_session)
    assert (stored.provider, stored.event_id) == ("SIMULATOR", incoming.event_id)
    assert stored.outcome is WebhookOutcome.APPLIED
    assert (stored.detail, stored.payment_id, stored.processed_at) == (None, payment.id, NOW)
    assert stored.payload["status"] == "SUCCESS"


# ==== the four layers of idempotency (architecture §6.2)


def test_the_same_event_twice_is_a_duplicate_and_changes_nothing(
    db_session: Session, make_service: ServiceFactory
) -> None:
    """Layer 1, AB11 in sequence."""
    payment = pending(db_session)
    incoming = event(payment.id, provider_payment_id="sim_1")
    service = make_service()
    assert service.handle_webhook(incoming) == processed(WebhookOutcome.APPLIED)
    assert service.handle_webhook(incoming) == WebhookAck(status="duplicate")
    assert len(events(db_session)) == 1
    assert state(db_session, payment) == (PaymentStatus.SUCCESS, BookingStatus.CONFIRMED)


def test_two_events_with_the_same_result_and_reference_settle_once(
    db_session: Session, make_service: ServiceFactory
) -> None:
    """Layer 2: different event ids, same result. The second finds the payment already settled."""
    payment = pending(db_session)
    service = make_service()
    first = service.handle_webhook(event(payment.id, provider_payment_id="sim_1"))
    second = service.handle_webhook(event(payment.id, provider_payment_id="sim_1"))
    assert first == processed(WebhookOutcome.APPLIED)
    assert second == processed(WebhookOutcome.IGNORED_NOOP)
    assert len(events(db_session)) == 2  # every signed and valid event is stored


def test_an_event_without_a_reference_repeating_a_settled_result_is_a_no_op(
    db_session: Session, make_service: ServiceFactory
) -> None:
    payment = pending(db_session)
    service = make_service()
    service.handle_webhook(event(payment.id, provider_payment_id="sim_1"))
    assert service.handle_webhook(event(payment.id)) == processed(WebhookOutcome.IGNORED_NOOP)


def test_a_second_reference_for_the_same_result_needs_review_and_changes_nothing(
    db_session: Session, make_service: ServiceFactory
) -> None:
    """Layer 3, AB23 in sequence: the provider names a second transaction."""
    payment = pending(db_session)
    service = make_service()
    service.handle_webhook(event(payment.id, provider_payment_id="sim_1"))
    second = service.handle_webhook(event(payment.id, provider_payment_id="sim_2"))
    assert second == processed(WebhookOutcome.NEEDS_REVIEW, "reference_mismatch")
    stored = db_session.get(Payment, payment.id)
    assert stored is not None
    db_session.refresh(stored)
    assert stored.provider_payment_id == "sim_1"


@pytest.mark.parametrize(("first", "second"), [("SUCCESS", "FAILED"), ("FAILED", "SUCCESS")])
def test_a_contradicting_result_needs_review_and_the_first_result_stands(
    db_session: Session, make_service: ServiceFactory, first: str, second: str
) -> None:
    """AB12."""
    payment = pending(db_session)
    service = make_service()
    service.handle_webhook(event(payment.id, status=first))
    ack = service.handle_webhook(event(payment.id, status=second))
    assert ack == processed(WebhookOutcome.NEEDS_REVIEW, "terminal_conflict")
    expected = (
        (PaymentStatus.SUCCESS, BookingStatus.CONFIRMED)
        if first == "SUCCESS"
        else (PaymentStatus.FAILED, BookingStatus.FAILED)
    )
    assert state(db_session, payment) == expected


def test_a_result_for_a_payment_that_never_had_a_reference_stores_the_first_one(
    db_session: Session, make_service: ServiceFactory
) -> None:
    booking = make_booking(db_session, status=BookingStatus.CONFIRMED)
    payment = make_payment(
        db_session, booking=booking, status=PaymentStatus.SUCCESS
    )  # no reference
    ack = make_service().handle_webhook(event(payment.id, provider_payment_id="sim_late"))
    assert ack == processed(WebhookOutcome.APPLIED)
    stored = db_session.get(Payment, payment.id)
    assert stored is not None
    db_session.refresh(stored)
    assert stored.provider_payment_id == "sim_late"


# ==== events that are stored and rejected


def test_an_event_for_an_unknown_payment_is_stored_rejected_and_a_redelivery_is_a_duplicate(
    db_session: Session, make_service: ServiceFactory
) -> None:
    """AB13."""
    incoming = event(uuid.uuid4())
    service = make_service()
    assert service.handle_webhook(incoming) == processed(WebhookOutcome.REJECTED, "unknown_payment")
    (stored,) = events(db_session)
    assert stored.outcome is WebhookOutcome.REJECTED
    assert (stored.detail, stored.payment_id) == ("unknown_payment", None)
    assert service.handle_webhook(incoming) == WebhookAck(status="duplicate")


def test_a_changed_amount_is_rejected_and_changes_nothing(
    db_session: Session, make_service: ServiceFactory
) -> None:
    """AB10."""
    payment = pending(db_session)
    ack = make_service().handle_webhook(event(payment.id, amount="1.00"))
    assert ack == processed(WebhookOutcome.REJECTED, "amount_mismatch")
    assert state(db_session, payment) == (PaymentStatus.PENDING, BookingStatus.PENDING)
    (stored,) = events(db_session)
    assert stored.payment_id == payment.id  # a known payment is recorded on its event


def test_another_currency_is_rejected_and_changes_nothing(
    db_session: Session, make_service: ServiceFactory
) -> None:
    payment = pending(db_session)
    ack = make_service().handle_webhook(event(payment.id, currency="USD"))
    assert ack == processed(WebhookOutcome.REJECTED, "currency_mismatch")
    assert state(db_session, payment) == (PaymentStatus.PENDING, BookingStatus.PENDING)


def test_a_reference_that_belongs_to_another_payment_is_rejected(
    db_session: Session, make_service: ServiceFactory
) -> None:
    make_payment(db_session, provider_payment_id="sim_taken")
    mine = pending(db_session)
    ack = make_service().handle_webhook(event(mine.id, provider_payment_id="sim_taken"))
    assert ack == processed(WebhookOutcome.REJECTED, "provider_reference_conflict")
    assert state(db_session, mine) == (PaymentStatus.PENDING, BookingStatus.PENDING)


def test_an_amount_that_equals_the_payment_up_to_the_scale_is_accepted(
    db_session: Session, make_service: ServiceFactory
) -> None:
    payment = make_payment(
        db_session, booking=make_booking(db_session, amount=Decimal("350")), amount=Decimal("350")
    )
    ack = make_service().handle_webhook(event(payment.id, amount="350"))
    assert ack == processed(WebhookOutcome.APPLIED)


# ==== the whole event is one transaction


def test_a_failure_rolls_everything_back_and_leaves_no_event_row(
    db_session: Session, make_service: ServiceFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec 8.2: if processing fails, the rollback removes the event row, so the provider's retry
    runs the event again. A gate that committed alone would mark it seen and never applied."""
    payment = pending(db_session)

    def explode(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("the process died half way")

    monkeypatch.setattr("app.payments.service.settle", explode)
    service = make_service()
    incoming = event(payment.id)
    with pytest.raises(RuntimeError):
        service.handle_webhook(incoming)
    assert events(db_session) == []
    assert state(db_session, payment) == (PaymentStatus.PENDING, BookingStatus.PENDING)
    monkeypatch.undo()
    assert service.handle_webhook(incoming) == processed(WebhookOutcome.APPLIED)  # the retry works


def test_the_transaction_has_ended_and_the_locks_are_free_afterwards(
    db_engine: Engine, db_session: Session, make_service: ServiceFactory
) -> None:
    payment = pending(db_session)
    service = make_service()
    service.handle_webhook(event(payment.id))
    with Session(db_engine) as prober:
        prober.execute(
            select(Booking.id).where(Booking.id == payment.booking_id).with_for_update(nowait=True)
        )
    assert service._session.in_transaction() is False


# ==== what an operator sees in the log


class SerializationFailure(Exception):
    """What psycopg puts in `orig`: PostgreSQL chose this transaction as the victim."""

    sqlstate = "40001"


def test_a_delivery_that_runs_out_of_retries_is_logged_with_its_ids_and_stores_nothing(
    db_session: Session, make_service: ServiceFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 503 carries no event ID, so this line is the operator's only trace of the event."""
    payment = pending(db_session)

    def victim(*_args: Any, **_kwargs: Any) -> None:
        raise OperationalError("UPDATE payments", {}, SerializationFailure())

    monkeypatch.setattr("app.payments.service.settle", victim)
    incoming = event(payment.id)
    with capture_logs() as logs, pytest.raises(ServiceUnavailable):
        make_service().handle_webhook(incoming)
    (line,) = [line for line in logs if line["event"] == "webhook.unavailable"]
    assert line["log_level"] == "warning"
    assert (line["event_id"], line["payment_id"]) == (incoming.event_id, str(payment.id))
    assert events(db_session) == []


def test_a_result_that_needs_review_is_logged_at_error_and_a_rejection_too(
    db_session: Session, make_service: ServiceFactory
) -> None:
    payment = pending(db_session)
    service = make_service()
    service.handle_webhook(event(payment.id, status="SUCCESS", provider_payment_id="sim_1"))
    with capture_logs() as logs:
        service.handle_webhook(event(payment.id, status="FAILED"))
        service.handle_webhook(event(payment.id, amount="1.00"))
        service.handle_webhook(event(uuid.uuid4()))
    review, rejected_amount, rejected_unknown = logs
    assert (review["event"], review["log_level"], review["detail"]) == (
        "webhook.needs_review",
        "error",
        "terminal_conflict",
    )
    assert (rejected_amount["event"], rejected_amount["detail"]) == (
        "webhook.rejected",
        "amount_mismatch",
    )
    assert rejected_amount["log_level"] == "error"  # the event broke an expectation
    assert rejected_unknown["detail"] == "unknown_payment"
    assert rejected_unknown["log_level"] == "warning"  # a delivery that raced ahead
    assert review["payment_id"] == str(payment.id)
    assert review["event_id"].startswith("evt_")
    for line in logs:  # the body of an event never goes to the log
        assert not {"amount", "currency", "provider_payment_id", "failure_reason"} & set(line)


def test_processed_and_duplicate_events_are_logged_at_info_and_warning(
    db_session: Session, make_service: ServiceFactory
) -> None:
    payment = pending(db_session)
    incoming = event(payment.id)
    service = make_service()
    with capture_logs() as logs:
        service.handle_webhook(incoming)
        service.handle_webhook(incoming)
    deliveries = [line for line in logs if line["event"].startswith("webhook.")]
    processed_line, duplicate_line = deliveries  # the settlement has its own events
    assert (processed_line["event"], processed_line["log_level"]) == ("webhook.processed", "info")
    assert processed_line["outcome"] == "APPLIED"
    assert (duplicate_line["event"], duplicate_line["log_level"]) == (
        "webhook.duplicate",
        "warning",
    )


# ==== the statement budget (architecture §5.2)


def test_the_statement_counts_are_eight_six_one_and_three(
    db_engine: Engine, db_session: Session, make_service: ServiceFactory
) -> None:
    """Applying a result: the event, the booking id, two locks, the reference check, three
    UPDATEs. A no-op skips the two writes. A duplicate is one probe."""
    payment = pending(db_session)
    service = make_service()

    def counted(incoming: WebhookPayload) -> int:
        with QueryCounter(db_engine) as counter:
            service.handle_webhook(incoming)
        return counter.count

    applying = event(payment.id, provider_payment_id="sim_1")
    assert counted(applying) == 8
    assert counted(event(payment.id, provider_payment_id="sim_1")) == 6  # a no-op
    assert counted(applying) == 1  # a duplicate
    assert counted(event(uuid.uuid4(), provider_payment_id="sim_x")) == 3  # an unknown payment

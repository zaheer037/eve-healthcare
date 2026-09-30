"""The provider simulator against the running app: duplicates and retries make one state change
(T4.7, architecture §6.4)."""

import json
import time
from collections.abc import Callable, Mapping
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.booking import Booking, BookingStatus
from app.models.payment import Payment, PaymentStatus, WebhookEvent
from scripts.simulate_provider import (
    Reply,
    Transport,
    build_event,
    deliver,
    deliver_many,
    main,
)
from tests.factories import make_booking, make_payment

URL = "http://api.test/payments/webhook/"
ClientFactory = Callable[..., TestClient]


def through(client: TestClient) -> Transport:
    """Send the request into the ASGI app in this process, as a real client would over a socket."""

    def send(url: str, body: bytes, headers: Mapping[str, str]) -> Reply:
        response = client.post(urlsplit(url).path, content=body, headers=dict(headers))
        return Reply(response.status_code, response.text)

    return send


@pytest.fixture
def secret(test_settings: Settings) -> str:
    return test_settings.webhook_secret.get_secret_value()


def statuses(db_session: Session, payment: Payment) -> tuple[PaymentStatus, BookingStatus]:
    db_session.expire_all()
    stored = db_session.get(Payment, payment.id)
    booking = db_session.get(Booking, payment.booking_id)
    assert stored is not None
    assert booking is not None
    return stored.status, booking.status


def count_events(db_session: Session) -> int:
    db_session.expire_all()
    return db_session.scalar(select(func.count()).select_from(WebhookEvent)) or 0


def test_five_duplicates_at_once_make_one_state_change(
    client: TestClient, secret: str, db_session: Session
) -> None:
    payment = make_payment(db_session, booking=make_booking(db_session))
    body = build_event(payment.id, "SUCCESS")
    deliveries = deliver_many(
        URL, secret, body, repeat=5, concurrent=True, send=through(client), max_attempts=1
    )
    answers = sorted(json.loads(delivery.reply.body)["status"] for delivery in deliveries)
    assert answers == ["duplicate"] * 4 + ["processed"]
    assert all(delivery.attempts == 1 for delivery in deliveries)
    assert count_events(db_session) == 1
    assert statuses(db_session, payment) == (PaymentStatus.SUCCESS, BookingStatus.CONFIRMED)


def test_a_delivery_that_meets_a_lock_timeout_is_retried_and_settles_once(
    client_with: ClientFactory, db_engine: Engine, secret: str, db_session: Session
) -> None:
    """The first attempt waits for a booking lock and gets 503. The rival finishes at that moment,
    the script backs off, and the retry finds the lock free. The event ID is the same, so the state
    changes once. The lock is released only after the 503 arrives, so no timing decides the test."""
    client = client_with(db_lock_timeout_ms=200)
    payment = make_payment(db_session, booking=make_booking(db_session))
    body = build_event(payment.id, "SUCCESS")
    rival = Session(db_engine)
    rival.execute(
        select(Booking.id).where(Booking.id == payment.booking_id).with_for_update(key_share=True)
    )
    transport = through(client)
    seen: list[int | None] = []

    def send(url: str, request_body: bytes, headers: Mapping[str, str]) -> Reply:
        reply = transport(url, request_body, headers)
        seen.append(reply.status)
        if reply.status == 503:
            rival.rollback()  # the rival's transaction ends, and its lock with it
        return reply

    try:
        delivery = deliver(URL, secret, body, send=send, jitter=lambda: 0.0, sleep=time.sleep)
    finally:
        rival.close()
    assert seen == [503, 200]
    assert delivery.attempts == 2
    assert json.loads(delivery.reply.body)["outcome"] == "APPLIED"
    assert count_events(db_session) == 1  # the first attempt left no event behind
    assert statuses(db_session, payment) == (PaymentStatus.SUCCESS, BookingStatus.CONFIRMED)


def test_a_wrong_signature_is_not_retried(client: TestClient, db_session: Session) -> None:
    payment = make_payment(db_session, booking=make_booking(db_session))
    body = build_event(payment.id, "SUCCESS")
    delivery = deliver(
        URL, "another-secret-of-the-same-length-0123456789", body, send=through(client)
    )
    assert (delivery.attempts, delivery.reply.status) == (1, 401)
    assert count_events(db_session) == 0


def test_the_command_settles_a_payment_and_reports_each_copy(
    client: TestClient,
    secret: str,
    db_session: Session,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payment = make_payment(db_session, booking=make_booking(db_session))
    code = main(
        ["--payment-id", str(payment.id), "--secret", secret, "--url", URL, "--repeat", "3"],
        send=through(client),
    )
    output = capsys.readouterr().out
    assert code == 0
    assert output.count("HTTP 200") == 3
    assert output.count('"status":"duplicate"') == 2
    assert statuses(db_session, payment) == (PaymentStatus.SUCCESS, BookingStatus.CONFIRMED)
    assert secret not in output


def test_a_failed_result_from_the_command_fails_the_booking(
    client: TestClient, secret: str, db_session: Session
) -> None:
    payment = make_payment(db_session, booking=make_booking(db_session))
    code = main(
        ["--payment-id", str(payment.id), "--secret", secret, "--url", URL, "--result", "FAILED"],
        send=through(client),
    )
    assert code == 0  # the delivery worked, whatever the payment result says
    assert statuses(db_session, payment) == (PaymentStatus.FAILED, BookingStatus.FAILED)

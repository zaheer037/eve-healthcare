"""Concurrent webhook deliveries: the idempotency that the design exists for (spec section 8.2).

Success criterion SC2 and checks AB11, AB23 and AB24.
"""

import json
import time
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.booking import Booking, BookingStatus
from app.models.payment import Payment, PaymentStatus, WebhookEvent
from app.payments.signature import header_for
from tests.factories import make_booking, make_payment
from tests.support import run_concurrently

JSON_HEADER = {"Content-Type": "application/json"}


@pytest.fixture
def secret(test_settings: Settings) -> str:
    return test_settings.webhook_secret.get_secret_value()


def raw(**fields: Any) -> bytes:
    body = {"event_id": f"evt_{uuid.uuid4().hex[:10]}", "status": "SUCCESS", **fields}
    return json.dumps(body, separators=(",", ":")).encode()


def deliver(client: TestClient, secret: str, body: bytes) -> Response:
    headers = {**JSON_HEADER, "X-Webhook-Signature": header_for(secret, int(time.time()), body)}
    return client.post("/payments/webhook/", content=body, headers=headers)


def pending_payment(db_session: Session) -> Payment:
    return make_payment(db_session, booking=make_booking(db_session))


def count(db_session: Session, model: type[WebhookEvent] | type[Payment]) -> int:
    db_session.expire_all()
    return db_session.scalar(select(func.count()).select_from(model)) or 0


def statuses(db_session: Session, payment: Payment) -> tuple[PaymentStatus, BookingStatus]:
    db_session.expire_all()
    stored = db_session.get(Payment, payment.id)
    booking = db_session.get(Booking, payment.booking_id)
    assert stored is not None
    assert booking is not None
    return stored.status, booking.status


@pytest.mark.concurrency
def test_twenty_identical_events_at_once_are_one_processed_and_nineteen_duplicates(
    client: TestClient, secret: str, db_session: Session
) -> None:
    payment = pending_payment(db_session)
    body = raw(payment_id=str(payment.id), event_id="evt_same", provider_payment_id="sim_1")
    calls = [lambda: deliver(client, secret, body).json()["status"] for _ in range(20)]
    results = run_concurrently(calls)
    assert sorted(results) == ["duplicate"] * 19 + ["processed"]
    assert count(db_session, WebhookEvent) == 1  # one event row
    assert count(db_session, Payment) == 1  # and still one payment
    assert statuses(db_session, payment) == (PaymentStatus.SUCCESS, BookingStatus.CONFIRMED)


@pytest.mark.concurrency
def test_ten_events_with_different_ids_and_references_are_one_applied_and_nine_reviews(
    client: TestClient, secret: str, db_session: Session
) -> None:
    """AB23: the booking lock serialises them, so the first wins and the rest disagree with it."""
    payment = pending_payment(db_session)
    calls = [
        lambda n=n: deliver(  # type: ignore[misc]
            client, secret, raw(payment_id=str(payment.id), provider_payment_id=f"sim_{n}")
        ).json()
        for n in range(10)
    ]
    results = run_concurrently(calls)
    outcomes = sorted((r["outcome"], r["detail"]) for r in results)
    assert outcomes == [("APPLIED", None)] + [("NEEDS_REVIEW", "reference_mismatch")] * 9
    assert count(db_session, WebhookEvent) == 10


@pytest.mark.concurrency
def test_one_reference_on_two_payments_at_the_same_moment_is_one_applied_and_one_rejected(
    client: TestClient, secret: str, db_session: Session
) -> None:
    """AB24: the pre-check cannot see an update that another transaction has not committed. The
    unique index stops the second, and the retry then records a rejection instead of a 500."""
    first, second = pending_payment(db_session), pending_payment(db_session)
    calls = [
        lambda payment=payment: deliver(  # type: ignore[misc]
            client, secret, raw(payment_id=str(payment.id), provider_payment_id="sim_shared")
        )
        for payment in (first, second)
    ]
    responses = run_concurrently(calls)
    assert [response.status_code for response in responses] == [200, 200]
    outcomes = sorted((r.json()["outcome"], r.json()["detail"]) for r in responses)
    assert outcomes == [("APPLIED", None), ("REJECTED", "provider_reference_conflict")]

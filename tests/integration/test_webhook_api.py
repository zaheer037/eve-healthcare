"""The webhook over HTTP (spec sections 7.4 and 8.5).

Covers AB8 to AB13, AB16, AB23, AB24 and AB32.
"""

import json
import time
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.booking import Booking, BookingStatus
from app.models.payment import Payment, PaymentStatus, WebhookEvent
from app.payments.signature import header_for
from tests.factories import make_booking, make_payment
from tests.support import QueryCounter

Headers = dict[str, str]
ClientFactory = Callable[..., TestClient]
SIGNATURE = "X-Webhook-Signature"
JSON_HEADER = {"Content-Type": "application/json"}


@pytest.fixture
def secret(test_settings: Settings) -> str:
    return test_settings.webhook_secret.get_secret_value()


def raw(**fields: Any) -> bytes:
    body = {"event_id": f"evt_{uuid.uuid4().hex[:10]}", "status": "SUCCESS", **fields}
    return json.dumps(body, separators=(",", ":")).encode()


def deliver(
    client: TestClient,
    secret: str | None,
    body: bytes,
    *,
    age: int = 0,
    path: str = "/payments/webhook/",
) -> Response:
    headers = dict(JSON_HEADER)
    if secret is not None:
        headers[SIGNATURE] = header_for(secret, int(time.time()) - age, body)
    return client.post(path, content=body, headers=headers)


def pending_payment(db_session: Session, **booking_fields: Any) -> Payment:
    return make_payment(db_session, booking=make_booking(db_session, **booking_fields))


def count_events(db_session: Session) -> int:
    db_session.expire_all()
    return db_session.scalar(select(func.count()).select_from(WebhookEvent)) or 0


def statuses(db_session: Session, payment: Payment) -> tuple[PaymentStatus, BookingStatus]:
    db_session.expire_all()
    stored = db_session.get(Payment, payment.id)
    booking = db_session.get(Booking, payment.booking_id)
    assert stored is not None
    assert booking is not None
    return stored.status, booking.status


# ==== a valid event


def test_a_signed_event_settles_the_payment_and_the_answer_says_so(
    client: TestClient, secret: str, db_session: Session
) -> None:
    payment = pending_payment(db_session)
    response = deliver(
        client,
        secret,
        raw(payment_id=str(payment.id), provider_payment_id="sim_1", amount="499.00"),
    )
    assert response.status_code == 200
    assert response.json() == {"status": "processed", "outcome": "APPLIED", "detail": None}
    assert statuses(db_session, payment) == (PaymentStatus.SUCCESS, BookingStatus.CONFIRMED)


def test_a_failed_event_fails_the_payment_and_the_booking(
    client: TestClient, secret: str, db_session: Session
) -> None:
    payment = pending_payment(db_session)
    response = deliver(
        client, secret, raw(payment_id=str(payment.id), status="FAILED", failure_reason="declined")
    )
    assert response.json()["outcome"] == "APPLIED"
    assert statuses(db_session, payment) == (PaymentStatus.FAILED, BookingStatus.FAILED)


def test_a_repeat_of_an_event_answers_duplicate(
    client: TestClient, secret: str, db_session: Session
) -> None:
    payment = pending_payment(db_session)
    body = raw(payment_id=str(payment.id))
    assert deliver(client, secret, body).json()["status"] == "processed"
    again = deliver(client, secret, body)
    assert again.status_code == 200
    assert again.json() == {"status": "duplicate", "outcome": None, "detail": None}
    assert count_events(db_session) == 1


def test_unknown_fields_are_ignored_and_the_route_answers_without_the_trailing_slash(
    client: TestClient, secret: str, db_session: Session
) -> None:
    payment = pending_payment(db_session)
    body = raw(payment_id=str(payment.id), brand_new_field={"a": 1}, booking_id=str(uuid.uuid4()))
    response = deliver(client, secret, body, path="/payments/webhook")  # no redirect (A13)
    assert response.status_code == 200
    assert response.json()["outcome"] == "APPLIED"


def test_the_webhook_needs_no_user_token(
    client: TestClient, secret: str, db_session: Session
) -> None:
    payment = pending_payment(db_session)
    response = deliver(client, secret, raw(payment_id=str(payment.id)))  # no Authorization header
    assert response.status_code == 200


# ==== the whole flow: pending mode, then the webhook (spec section 8.3)


def test_pending_mode_step_by_step(
    client: TestClient, secret: str, patient: Any, patient_headers: Headers, db_session: Session
) -> None:
    booking = make_booking(db_session, user=patient)
    paid = client.post(
        "/payments/",
        json={"booking_id": str(booking.id), "simulate": "PENDING"},
        headers=patient_headers,
    )
    payment_id = paid.json()["id"]
    assert paid.status_code == 202
    cancel = client.post(f"/bookings/{booking.id}/cancel", headers=patient_headers)
    assert (cancel.status_code, cancel.json()["code"]) == (409, "PAYMENT_IN_PROGRESS")
    replay = client.post(
        "/payments/", json={"booking_id": str(booking.id)}, headers=patient_headers
    )
    assert (replay.status_code, replay.json()["id"]) == (202, payment_id)
    settled = deliver(client, secret, raw(payment_id=payment_id, provider_payment_id="sim_late"))
    assert settled.json()["outcome"] == "APPLIED"
    read = client.get(f"/payments/{payment_id}", headers=patient_headers).json()
    assert (read["status"], read["booking"]["status"]) == ("SUCCESS", "CONFIRMED")
    after = client.post("/payments/", json={"booking_id": str(booking.id)}, headers=patient_headers)
    assert (after.status_code, after.json()["status"]) == (200, "SUCCESS")
    cancelled = client.post(f"/bookings/{booking.id}/cancel", headers=patient_headers)
    assert cancelled.status_code == 200  # the payment is settled, so the cancel works now


# ==== the signature (AB8, AB9)


def test_a_missing_or_wrong_or_stale_signature_is_401_and_changes_nothing(
    client: TestClient, secret: str, db_session: Session
) -> None:
    payment = pending_payment(db_session)
    body = raw(payment_id=str(payment.id))
    attempts = {
        "missing": deliver(client, None, body),
        "wrong secret": deliver(client, "another-secret-of-the-same-length-0123456789", body),
        "stale": deliver(client, secret, body, age=400),
        "future": deliver(client, secret, body, age=-400),
    }
    for name, response in attempts.items():
        assert response.status_code == 401, name
        assert response.json()["code"] == "WEBHOOK_SIGNATURE_INVALID"
        assert response.headers["www-authenticate"] == 'Signature realm="webhook"'
    assert count_events(db_session) == 0
    assert statuses(db_session, payment) == (PaymentStatus.PENDING, BookingStatus.PENDING)


def test_a_body_changed_after_signing_is_401(
    client: TestClient, secret: str, db_session: Session
) -> None:
    payment = pending_payment(db_session)
    signed_for = raw(payment_id=str(payment.id), event_id="evt_signed")
    headers = {**JSON_HEADER, SIGNATURE: header_for(secret, int(time.time()), signed_for)}
    tampered = signed_for.replace(b"SUCCESS", b"FAILED")
    assert client.post("/payments/webhook/", content=tampered, headers=headers).status_code == 401
    assert count_events(db_session) == 0


def test_the_check_can_be_switched_off_outside_production(
    client_with: ClientFactory, db_session: Session
) -> None:
    client = client_with(webhook_signature_required=False)
    payment = pending_payment(db_session)
    response = deliver(client, None, raw(payment_id=str(payment.id)))
    assert response.status_code == 200


# ==== a body that is too big (AB16)


def test_a_body_over_the_cap_is_413_with_a_content_length_and_stores_nothing(
    client: TestClient, secret: str, db_session: Session
) -> None:
    padding = "x" * 70_000
    body = raw(payment_id=str(uuid.uuid4()), failure_reason=padding)
    response = deliver(client, secret, body)
    assert (response.status_code, response.json()["code"]) == (413, "PAYLOAD_TOO_LARGE")
    assert count_events(db_session) == 0


def test_a_chunked_body_over_the_cap_is_413_too(
    client: TestClient, secret: str, db_session: Session
) -> None:
    def chunks() -> Iterator[bytes]:
        yield b'{"event_id":"'
        for _ in range(3):
            yield b"x" * 30_000
        yield b'"}'

    headers = {**JSON_HEADER, SIGNATURE: header_for(secret, int(time.time()), b"anything")}
    response = client.post("/payments/webhook/", content=chunks(), headers=headers)
    assert response.status_code == 413
    assert count_events(db_session) == 0


# ==== a signed body that is not a valid event (AB32)


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"status": "REFUNDED"}, "body.status"),
        ({"event_id": None}, "body.event_id"),
        ({"event_id": "a\u0000b"}, "body.event_id"),
        ({"provider_payment_id": "sim\u0000"}, "body.provider_payment_id"),
        ({"failure_reason": "declined\u0000"}, "body.failure_reason"),
        ({"amount": "-1"}, "body.amount"),
        ({"currency": "inr"}, "body.currency"),
        ({"payment_id": "nope"}, "body.payment_id"),
    ],
)
def test_a_signed_body_with_a_bad_field_is_422_and_stores_no_event(
    client: TestClient,
    secret: str,
    db_session: Session,
    changes: dict[str, Any],
    field: str,
) -> None:
    payment = pending_payment(db_session)
    fields = {"payment_id": str(payment.id), **changes}
    fields = {key: value for key, value in fields.items() if value is not None}
    if changes.get("event_id", "present") is None:  # the "missing event_id" case
        body = json.dumps({"payment_id": fields["payment_id"], "status": "SUCCESS"}).encode()
    else:
        body = raw(**fields)
    response = deliver(client, secret, body)
    assert response.status_code == 422
    assert [item["field"] for item in response.json()["errors"]] == [field]
    assert count_events(db_session) == 0
    assert statuses(db_session, payment) == (PaymentStatus.PENDING, BookingStatus.PENDING)


@pytest.mark.parametrize("body", [b"", b"not json", b"[]", b"\xff\xfe\x00"])
def test_a_signed_body_that_is_not_an_event_object_is_422(
    client: TestClient, secret: str, db_session: Session, body: bytes
) -> None:
    assert deliver(client, secret, body).status_code == 422
    assert count_events(db_session) == 0


# ==== events that are stored and rejected


def test_an_event_for_an_unknown_payment_is_200_rejected_and_stored(
    client: TestClient, secret: str, db_session: Session
) -> None:
    """AB13."""
    body = raw(payment_id=str(uuid.uuid4()))
    response = deliver(client, secret, body)
    assert response.json() == {
        "status": "processed",
        "outcome": "REJECTED",
        "detail": "unknown_payment",
    }
    assert count_events(db_session) == 1
    assert deliver(client, secret, body).json()["status"] == "duplicate"


def test_a_changed_amount_is_200_rejected_and_changes_nothing(
    client: TestClient, secret: str, db_session: Session
) -> None:
    """AB10."""
    payment = pending_payment(db_session)
    response = deliver(client, secret, raw(payment_id=str(payment.id), amount="1.00"))
    assert response.status_code == 200
    assert (response.json()["outcome"], response.json()["detail"]) == (
        "REJECTED",
        "amount_mismatch",
    )
    assert statuses(db_session, payment) == (PaymentStatus.PENDING, BookingStatus.PENDING)


def test_a_late_failure_after_a_success_needs_review_and_the_booking_stays_confirmed(
    client: TestClient, secret: str, db_session: Session
) -> None:
    """AB12."""
    payment = pending_payment(db_session)
    deliver(client, secret, raw(payment_id=str(payment.id), status="SUCCESS"))
    late = deliver(client, secret, raw(payment_id=str(payment.id), status="FAILED"))
    assert (late.json()["outcome"], late.json()["detail"]) == ("NEEDS_REVIEW", "terminal_conflict")
    assert statuses(db_session, payment) == (PaymentStatus.SUCCESS, BookingStatus.CONFIRMED)


# ==== a lock timeout is a 503 and is not retried


def test_a_lock_timeout_is_503_asks_for_a_retry_stores_nothing_and_is_not_retried(
    client_with: ClientFactory, db_engine: Engine, secret: str, db_session: Session
) -> None:
    client = client_with(db_lock_timeout_ms=300)
    payment = pending_payment(db_session)
    body = raw(payment_id=str(payment.id))
    with Session(db_engine) as holder:
        holder.execute(
            select(Booking.id)
            .where(Booking.id == payment.booking_id)
            .with_for_update(key_share=True)
        )
        with QueryCounter(client.app.state.engine) as counter:  # type: ignore[attr-defined]
            response = deliver(client, secret, body)
    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert response.json()["code"] == "SERVICE_UNAVAILABLE"
    inserts = [s for s in counter.statements if "INSERT INTO webhook_events" in s]
    assert len(inserts) == 1  # one attempt: a lock timeout is not retried
    assert count_events(db_session) == 0  # rolled back, so the provider's retry runs the event
    retry = deliver(client, secret, body)  # the lock is free now
    assert retry.json()["outcome"] == "APPLIED"


# ==== the documentation of a route without a declared body


def test_the_openapi_document_lists_the_signature_header_and_the_body(client: TestClient) -> None:
    operation = client.get("/openapi.json").json()["paths"]["/payments/webhook/"]["post"]
    header = next(p for p in operation["parameters"] if p["name"] == "X-Webhook-Signature")
    assert header["in"] == "header"
    assert header["required"] is True
    schema = operation["requestBody"]["content"]["application/json"]["schema"]
    assert {"event_id", "payment_id", "status"} <= set(schema["required"])
    assert operation["requestBody"]["content"]["application/json"]["example"]["status"] == "SUCCESS"

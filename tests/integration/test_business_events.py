"""The business events of spec section 10.2: what each flow logs, and what it must not.

An event carries ids and never a body, an email, a secret or a row value (AB28).
"""

import json
import time
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.booking import BookingStatus
from app.models.payment import PaymentStatus
from app.payments.signature import header_for
from tests.factories import future_slot, make_booking, make_centre_test, make_payment

Headers = dict[str, str]
BUSINESS_EVENTS = {"booking.created", "booking.status_changed", "payment.settled"}
WEBHOOK_EVENTS = {
    "webhook.processed",
    "webhook.duplicate",
    "webhook.rejected",
    "webhook.needs_review",
    "webhook.invalid",
}


def named(logs: list[dict[str, Any]], *names: str) -> list[dict[str, Any]]:
    return [line for line in logs if line["event"] in names]


def business(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return named(logs, *BUSINESS_EVENTS, *WEBHOOK_EVENTS)


def deliver(client: TestClient, secret: str, **fields: Any) -> Response:
    body = json.dumps(
        {"event_id": f"evt_{uuid.uuid4().hex[:8]}", "status": "SUCCESS", **fields},
        separators=(",", ":"),
    ).encode()
    headers = {
        "Content-Type": "application/json",
        "X-Webhook-Signature": header_for(secret, int(time.time()), body),
    }
    return client.post("/payments/webhook/", content=body, headers=headers)


@pytest.fixture
def secret(test_settings: Settings) -> str:
    return test_settings.webhook_secret.get_secret_value()


# ==== bookings


def test_a_new_booking_is_logged_once_with_ids_and_the_request_id(
    client: TestClient,
    logs: list[dict[str, Any]],
    patient: Any,
    patient_headers: Headers,
    db_session: Session,
) -> None:
    offering = make_centre_test(db_session)
    response = client.post(
        "/bookings/",
        json={
            "centre_id": str(offering.centre_id),
            "test_id": str(offering.test_id),
            "appointment_at": future_slot().isoformat(),
        },
        headers=patient_headers,
    )
    (created,) = named(logs, "booking.created")
    assert created["log_level"] == "info"
    assert created["booking_id"] == response.json()["id"]
    assert created["user_id"] == str(patient.id)
    assert (created["centre_id"], created["test_id"]) == (
        str(offering.centre_id),
        str(offering.test_id),
    )
    # The service logged it in a pool thread, and the request ID still got there.
    assert created["request_id"] == response.headers["x-request-id"]


def test_a_booking_that_was_refused_logs_no_event(
    client: TestClient,
    logs: list[dict[str, Any]],
    patient_headers: Headers,
    db_session: Session,
) -> None:
    offering = make_centre_test(db_session)
    body = {
        "centre_id": str(offering.centre_id),
        "test_id": str(offering.test_id),
        "appointment_at": future_slot().isoformat(),
    }
    client.post("/bookings/", json=body, headers=patient_headers)
    logs.clear()
    assert client.post("/bookings/", json=body, headers=patient_headers).status_code == 409
    assert client.post("/bookings/", json={}, headers=patient_headers).status_code == 422
    assert business(logs) == []


def test_a_cancel_logs_the_change_and_a_repeat_logs_nothing(
    client: TestClient,
    logs: list[dict[str, Any]],
    patient: Any,
    patient_headers: Headers,
    db_session: Session,
) -> None:
    booking = make_booking(db_session, user=patient)
    first = client.post(f"/bookings/{booking.id}/cancel", headers=patient_headers)
    (changed,) = named(logs, "booking.status_changed")
    assert changed["booking_id"] == str(booking.id)
    assert changed["user_id"] == str(patient.id)
    assert (changed["from_status"], changed["to_status"], changed["cause"]) == (
        "PENDING",
        "CANCELLED",
        "cancel",
    )
    assert changed["request_id"] == first.headers["x-request-id"]
    logs.clear()
    client.post(f"/bookings/{booking.id}/cancel", headers=patient_headers)  # a no-op
    assert business(logs) == []


# ==== payments


def pay(client: TestClient, headers: Headers, booking_id: uuid.UUID, **fields: Any) -> Response:
    return client.post(
        "/payments/", json={"booking_id": str(booking_id), **fields}, headers=headers
    )


def test_a_successful_payment_logs_the_settlement_and_the_booking_change(
    client: TestClient,
    logs: list[dict[str, Any]],
    patient: Any,
    patient_headers: Headers,
    db_session: Session,
) -> None:
    booking = make_booking(db_session, user=patient)
    response = pay(client, patient_headers, booking.id, simulate="SUCCESS")
    (settled,) = named(logs, "payment.settled")
    assert settled["payment_id"] == response.json()["id"]
    assert settled["booking_id"] == str(booking.id)
    assert (settled["status"], settled["source"], settled["failure_reason"]) == (
        "SUCCESS",
        "api",
        None,
    )
    (changed,) = named(logs, "booking.status_changed")
    assert (changed["from_status"], changed["to_status"], changed["cause"]) == (
        "PENDING",
        "CONFIRMED",
        "payment",
    )
    assert {line["request_id"] for line in (settled, changed)} == {response.headers["x-request-id"]}


def test_a_failed_payment_logs_why_it_failed(
    client: TestClient,
    logs: list[dict[str, Any]],
    patient: Any,
    patient_headers: Headers,
    db_session: Session,
) -> None:
    booking = make_booking(db_session, user=patient)
    pay(client, patient_headers, booking.id, simulate="FAILED")
    (settled,) = named(logs, "payment.settled")
    assert (settled["status"], settled["source"]) == ("FAILED", "api")
    assert settled["failure_reason"] == "declined_by_simulator"
    (changed,) = named(logs, "booking.status_changed")
    assert (changed["from_status"], changed["to_status"]) == ("PENDING", "FAILED")


def test_a_pending_payment_and_a_replay_log_no_settlement(
    client: TestClient,
    logs: list[dict[str, Any]],
    patient: Any,
    patient_headers: Headers,
    db_session: Session,
) -> None:
    booking = make_booking(db_session, user=patient)
    pay(client, patient_headers, booking.id, simulate="PENDING")
    pay(client, patient_headers, booking.id)  # a replay of the pending payment
    assert business(logs) == []


# ==== the webhook


def test_an_applied_event_logs_the_delivery_the_settlement_and_the_booking_change(
    client: TestClient,
    logs: list[dict[str, Any]],
    secret: str,
    db_session: Session,
) -> None:
    payment = make_payment(db_session, booking=make_booking(db_session))
    response = deliver(client, secret, payment_id=str(payment.id), provider_payment_id="sim_1")
    names = sorted(line["event"] for line in business(logs))
    assert names == ["booking.status_changed", "payment.settled", "webhook.processed"]
    (settled,) = named(logs, "payment.settled")
    assert (settled["status"], settled["source"], settled["payment_id"]) == (
        "SUCCESS",
        "webhook",
        str(payment.id),
    )
    (changed,) = named(logs, "booking.status_changed")
    assert (changed["from_status"], changed["to_status"], changed["cause"]) == (
        "PENDING",
        "CONFIRMED",
        "webhook",
    )
    for line in business(logs):
        assert line["request_id"] == response.headers["x-request-id"]


def test_a_no_op_and_a_duplicate_log_only_the_delivery(
    client: TestClient,
    logs: list[dict[str, Any]],
    secret: str,
    db_session: Session,
) -> None:
    payment = make_payment(db_session, booking=make_booking(db_session))
    deliver(client, secret, payment_id=str(payment.id), event_id="evt_same")
    logs.clear()
    deliver(client, secret, payment_id=str(payment.id), event_id="evt_same")  # duplicate
    deliver(client, secret, payment_id=str(payment.id))  # a new event id, the same result
    assert [line["event"] for line in business(logs)] == ["webhook.duplicate", "webhook.processed"]
    assert named(logs, "webhook.processed")[0]["outcome"] == "IGNORED_NOOP"


def test_a_contradicting_event_logs_needs_review_and_no_settlement(
    client: TestClient,
    logs: list[dict[str, Any]],
    secret: str,
    db_session: Session,
) -> None:
    payment = make_payment(db_session, booking=make_booking(db_session))
    deliver(client, secret, payment_id=str(payment.id), status="SUCCESS")
    logs.clear()
    deliver(client, secret, payment_id=str(payment.id), status="FAILED")
    (review,) = business(logs)
    assert (review["event"], review["log_level"], review["detail"]) == (
        "webhook.needs_review",
        "error",
        "terminal_conflict",
    )


def test_a_signed_body_that_fails_validation_logs_the_field_names_only(
    client: TestClient,
    logs: list[dict[str, Any]],
    secret: str,
    db_session: Session,
) -> None:
    payment = make_payment(db_session, booking=make_booking(db_session))
    deliver(client, secret, payment_id=str(payment.id), status="REFUNDED", currency="inr")
    (invalid,) = named(logs, "webhook.invalid")
    assert invalid["log_level"] == "warning"
    assert sorted(invalid["fields"]) == ["body.currency", "body.status"]
    assert "REFUNDED" not in json.dumps(invalid)  # no value, only names


# ==== what the events never hold


def test_no_event_of_a_whole_flow_holds_an_email_a_secret_a_token_or_a_body(
    client: TestClient,
    logs: list[dict[str, Any]],
    secret: str,
    db_session: Session,
) -> None:
    email, password = "audit-flow@example.in", "a long enough password 123"
    client.post("/auth/signup", json={"email": email, "password": password})
    token = client.post("/auth/login", json={"email": email, "password": password}).json()[
        "access_token"
    ]
    headers = {"Authorization": f"Bearer {token}"}
    offering = make_centre_test(db_session)
    booked = client.post(
        "/bookings/",
        json={
            "centre_id": str(offering.centre_id),
            "test_id": str(offering.test_id),
            "appointment_at": future_slot().isoformat(),
        },
        headers=headers,
    ).json()
    payment_id = pay(client, headers, uuid.UUID(booked["id"]), simulate="PENDING").json()["id"]
    deliver(client, secret, payment_id=payment_id, provider_payment_id="sim_secret_reference")
    client.get("/centres/", params={"q": "needle-in-the-query-string"})
    everything = json.dumps(logs, default=str)
    for forbidden in (
        email,
        password,
        token,
        secret,
        "sim_secret_reference",
        "needle-in-the-query-string",
        "$argon2",
    ):
        assert forbidden not in everything, forbidden
    assert BookingStatus.CONFIRMED.value in everything  # the events themselves are there
    assert PaymentStatus.SUCCESS.value in everything

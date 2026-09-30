"""Paying a booking and reading a payment over HTTP (architecture §§5.4 and 6.1).

Covers AB4, AB5, AB20, AB30 and AB31.
"""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.booking import Booking, BookingStatus
from app.models.payment import Payment, PaymentStatus
from tests.factories import future_slot, make_booking, make_payment, make_user
from tests.support import AUTH_LOOKUP, QueryCounter, run_concurrently

Headers = dict[str, str]
ClientFactory = Callable[..., TestClient]


def pay(client: TestClient, headers: Headers | None, booking: Booking, **extra: Any) -> Response:
    return client.post("/payments/", json={"booking_id": str(booking.id), **extra}, headers=headers)


def count_payments(db_session: Session) -> int:
    return db_session.scalar(select(func.count()).select_from(Payment)) or 0


def booking_status(db_session: Session, booking: Booking) -> BookingStatus:
    db_session.expire_all()
    found = db_session.get(Booking, booking.id)
    assert found is not None
    return found.status


# ==== who may pay


def test_paying_needs_a_token(client: TestClient, patient: Any, db_session: Session) -> None:
    booking = make_booking(db_session, user=patient)
    response = pay(client, None, booking)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_user_b_cannot_pay_user_as_booking(
    client: TestClient, patient_headers: Headers, db_session: Session
) -> None:
    """AB4."""
    booking = make_booking(db_session, user=make_user(db_session))
    response = pay(client, patient_headers, booking, simulate="SUCCESS")
    assert (response.status_code, response.json()["code"]) == (403, "FORBIDDEN")
    assert count_payments(db_session) == 0
    assert booking_status(db_session, booking) is BookingStatus.PENDING


def test_an_unknown_booking_is_404_and_a_malformed_id_is_422(
    client: TestClient, patient_headers: Headers
) -> None:
    unknown = client.post(
        "/payments/", json={"booking_id": str(uuid.uuid4())}, headers=patient_headers
    )
    assert (unknown.status_code, unknown.json()["code"]) == (404, "BOOKING_NOT_FOUND")
    malformed = client.post("/payments/", json={"booking_id": "nope"}, headers=patient_headers)
    assert malformed.status_code == 422
    assert [item["field"] for item in malformed.json()["errors"]] == ["body.booking_id"]


# ==== the three results


def test_a_successful_payment_is_201_confirms_the_booking_and_shows_on_it(
    client: TestClient, patient: Any, patient_headers: Headers, db_session: Session
) -> None:
    booking = make_booking(db_session, user=patient)
    response = pay(client, patient_headers, booking, simulate="SUCCESS")
    body = response.json()
    assert response.status_code == 201
    assert response.headers["location"] == f"/payments/{body['id']}"
    assert set(body) == {
        "id",
        "amount",
        "currency",
        "status",
        "failure_reason",
        "settled_at",
        "booking_id",
        "booking",
    }
    assert (body["status"], body["amount"], body["currency"]) == ("SUCCESS", "499.00", "INR")
    assert body["booking"] == {"id": str(booking.id), "status": "CONFIRMED"}
    assert body["settled_at"].endswith("Z")
    shown = client.get(f"/bookings/{booking.id}", headers=patient_headers).json()
    assert shown["status"] == "CONFIRMED"
    assert [payment["id"] for payment in shown["payments"]] == [body["id"]]


def test_a_failed_payment_is_201_fails_the_booking_and_a_retry_is_409(
    client: TestClient, patient: Any, patient_headers: Headers, db_session: Session
) -> None:
    booking = make_booking(db_session, user=patient)
    response = pay(client, patient_headers, booking, simulate="FAILED")
    assert response.status_code == 201
    assert response.json()["status"] == "FAILED"
    assert response.json()["failure_reason"]
    assert response.json()["booking"]["status"] == "FAILED"
    retry = pay(client, patient_headers, booking)
    assert (retry.status_code, retry.json()["code"]) == (409, "BOOKING_NOT_PAYABLE")


def test_a_pending_payment_is_202_and_keeps_the_booking_pending(
    client: TestClient, patient: Any, patient_headers: Headers, db_session: Session
) -> None:
    booking = make_booking(db_session, user=patient)
    response = pay(client, patient_headers, booking, simulate="PENDING")
    assert response.status_code == 202
    assert response.json()["status"] == "PENDING"
    assert response.json()["settled_at"] is None
    assert response.json()["booking"]["status"] == "PENDING"
    assert response.headers["location"] == f"/payments/{response.json()['id']}"


def test_a_pending_payment_blocks_a_cancel_and_a_retry_replays_it(
    client: TestClient, patient: Any, patient_headers: Headers, db_session: Session
) -> None:
    booking = make_booking(db_session, user=patient)
    first = pay(client, patient_headers, booking, simulate="PENDING")
    cancel = client.post(f"/bookings/{booking.id}/cancel", headers=patient_headers)
    assert (cancel.status_code, cancel.json()["code"]) == (409, "PAYMENT_IN_PROGRESS")
    retry = pay(client, patient_headers, booking)
    assert retry.status_code == 202
    assert retry.json()["id"] == first.json()["id"]
    assert "location" not in retry.headers  # a replay created nothing


# ==== a replay


def test_a_repeat_after_success_is_200_with_the_same_payment(
    client: TestClient, patient: Any, patient_headers: Headers, db_session: Session
) -> None:
    booking = make_booking(db_session, user=patient)
    first = pay(client, patient_headers, booking, simulate="SUCCESS")
    again = pay(client, patient_headers, booking)
    assert again.status_code == 200
    assert again.json() == first.json()
    assert "location" not in again.headers
    assert count_payments(db_session) == 1


def test_a_cancelled_booking_that_kept_its_payment_replays_it(
    client: TestClient, patient: Any, patient_headers: Headers, db_session: Session
) -> None:
    booking = make_booking(db_session, user=patient, status=BookingStatus.CANCELLED)
    payment = make_payment(db_session, booking=booking, status=PaymentStatus.SUCCESS)
    response = pay(client, patient_headers, booking)
    assert response.status_code == 200
    assert response.json()["id"] == str(payment.id)
    assert response.json()["booking"]["status"] == "CANCELLED"


@pytest.mark.concurrency
def test_ten_payments_at_once_create_one_payment_and_replay_it_nine_times(
    client: TestClient, patient: Any, patient_headers: Headers, db_session: Session
) -> None:
    booking = make_booking(db_session, user=patient)

    def attempt() -> tuple[int, str]:
        response = pay(client, patient_headers, booking, simulate="SUCCESS")
        return response.status_code, response.json()["id"]

    results = run_concurrently([attempt for _ in range(10)])
    assert sorted(status for status, _ in results) == [200] * 9 + [201]
    assert len({payment_id for _, payment_id in results}) == 1
    assert count_payments(db_session) == 1
    assert booking_status(db_session, booking) is BookingStatus.CONFIRMED


# ==== what cannot be paid


@pytest.mark.parametrize("status", [BookingStatus.CANCELLED, BookingStatus.FAILED])
def test_a_cancelled_or_failed_booking_with_no_payment_is_409(
    client: TestClient,
    patient: Any,
    patient_headers: Headers,
    db_session: Session,
    status: BookingStatus,
) -> None:
    """AB20."""
    booking = make_booking(db_session, user=patient, status=status)
    response = pay(client, patient_headers, booking)
    assert (response.status_code, response.json()["code"]) == (409, "BOOKING_NOT_PAYABLE")
    assert status.value in response.json()["detail"]
    assert count_payments(db_session) == 0


def test_a_booking_whose_appointment_has_started_is_409(
    client: TestClient, patient: Any, patient_headers: Headers, db_session: Session
) -> None:
    """AB31."""
    booking = make_booking(
        db_session, user=patient, appointment_at=datetime.now(UTC) - timedelta(minutes=5)
    )
    response = pay(client, patient_headers, booking)
    assert (response.status_code, response.json()["code"]) == (409, "BOOKING_NOT_PAYABLE")
    assert count_payments(db_session) == 0


# ==== input


@pytest.mark.parametrize("field", ["amount", "status", "currency", "user_id"])
def test_a_client_never_sets_the_amount_or_any_other_server_field(
    client: TestClient,
    patient: Any,
    patient_headers: Headers,
    db_session: Session,
    field: str,
) -> None:
    booking = make_booking(db_session, user=patient)
    response = pay(client, patient_headers, booking, **{field: 1})
    assert response.status_code == 422
    assert [item["field"] for item in response.json()["errors"]] == [f"body.{field}"]
    assert count_payments(db_session) == 0


@pytest.mark.parametrize("simulate", ["success", "REFUNDED", "", 1])
def test_an_unknown_simulated_result_is_422(
    client: TestClient,
    patient: Any,
    patient_headers: Headers,
    db_session: Session,
    simulate: Any,
) -> None:
    booking = make_booking(db_session, user=patient)
    assert pay(client, patient_headers, booking, simulate=simulate).status_code == 422


def test_the_body_is_checked_after_the_token(client: TestClient) -> None:
    assert client.post("/payments/", json={}).status_code == 401


# ==== the simulation switch and the success rate


def test_a_forced_result_is_422_when_the_setting_forbids_it(
    client_with: ClientFactory, patient: Any, patient_headers: Headers, db_session: Session
) -> None:
    """AB30: production turns the override off."""
    client = client_with(payment_allow_simulate_override=False)
    booking = make_booking(db_session, user=patient)
    response = pay(client, patient_headers, booking, simulate="SUCCESS")
    assert (response.status_code, response.json()["code"]) == (422, "SIMULATION_NOT_ALLOWED")
    assert count_payments(db_session) == 0


@pytest.mark.parametrize(("rate", "expected"), [(1.0, "SUCCESS"), (0.0, "FAILED")])
def test_without_an_override_the_success_rate_setting_decides(
    client_with: ClientFactory,
    patient: Any,
    patient_headers: Headers,
    db_session: Session,
    *,
    rate: float,
    expected: str,
) -> None:
    client = client_with(payment_success_rate=rate, payment_allow_simulate_override=False)
    booking = make_booking(db_session, user=patient)
    response = pay(client, patient_headers, booking)
    assert response.status_code == 201
    assert response.json()["status"] == expected


def test_without_simulate_a_payment_always_ends_in_success_or_failed(
    client: TestClient, patient: Any, patient_headers: Headers, db_session: Session
) -> None:
    """With the default success rate, the dice decide, and the answer is
    always a settled payment: 201, SUCCESS or FAILED, and never a pending 202."""
    bookings = [
        make_booking(db_session, user=patient, appointment_at=future_slot(2 + day))
        for day in range(15)
    ]
    responses = [pay(client, patient_headers, booking) for booking in bookings]
    assert {response.status_code for response in responses} == {201}
    assert {response.json()["status"] for response in responses} <= {"SUCCESS", "FAILED"}
    for booking, response in zip(bookings, responses, strict=True):
        expected = (
            BookingStatus.CONFIRMED
            if response.json()["status"] == "SUCCESS"
            else BookingStatus.FAILED
        )
        assert booking_status(db_session, booking) is expected  # the booking follows the payment


# ==== read one payment


def test_an_owner_reads_a_payment_with_its_booking_status(
    client: TestClient, patient: Any, patient_headers: Headers, db_session: Session
) -> None:
    booking = make_booking(db_session, user=patient, status=BookingStatus.CONFIRMED)
    payment = make_payment(db_session, booking=booking, status=PaymentStatus.SUCCESS)
    response = client.get(f"/payments/{payment.id}", headers=patient_headers)
    assert response.status_code == 200
    assert response.json()["id"] == str(payment.id)
    assert response.json()["booking"] == {"id": str(booking.id), "status": "CONFIRMED"}


def test_user_b_cannot_read_user_as_payment(
    client: TestClient, patient_headers: Headers, db_session: Session
) -> None:
    """AB5."""
    payment = make_payment(db_session, booking=make_booking(db_session, user=make_user(db_session)))
    response = client.get(f"/payments/{payment.id}", headers=patient_headers)
    assert (response.status_code, response.json()["code"]) == (403, "FORBIDDEN")


def test_an_unknown_payment_is_404_a_malformed_id_is_422_and_no_token_is_401(
    client: TestClient, patient_headers: Headers
) -> None:
    unknown = client.get(f"/payments/{uuid.uuid4()}", headers=patient_headers)
    assert (unknown.status_code, unknown.json()["code"]) == (404, "PAYMENT_NOT_FOUND")
    assert client.get("/payments/not-a-uuid", headers=patient_headers).status_code == 422
    assert client.get(f"/payments/{uuid.uuid4()}").status_code == 401


# ==== the statement budgets (architecture §5.2)


def test_the_statement_budgets_hold_over_http(
    app: FastAPI,
    client: TestClient,
    patient: Any,
    patient_headers: Headers,
    db_session: Session,
) -> None:
    settled = make_booking(db_session, user=patient)
    pending = make_booking(db_session, user=patient)
    stranger_payment = make_payment(
        db_session, booking=make_booking(db_session, user=make_user(db_session))
    )
    payment_id = ""
    with QueryCounter(app.state.engine, ignore=(AUTH_LOOKUP,)) as counter:

        def counted(response_status: int, budget: int, action: Callable[[], Response]) -> Response:
            counter.statements.clear()
            response = action()
            assert response.status_code == response_status, response.text
            assert counter.count == budget, (budget, counter.statements)
            return response

        created = counted(201, 4, lambda: pay(client, patient_headers, settled, simulate="SUCCESS"))
        payment_id = created.json()["id"]
        counted(202, 3, lambda: pay(client, patient_headers, pending, simulate="PENDING"))
        counted(200, 2, lambda: pay(client, patient_headers, settled))  # a replay
        counted(200, 1, lambda: client.get(f"/payments/{payment_id}", headers=patient_headers))
        counted(
            403, 2, lambda: client.get(f"/payments/{stranger_payment.id}", headers=patient_headers)
        )

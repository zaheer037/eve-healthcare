"""Races on one booking: pay against cancel, and the webhook against cancel (spec section 8.3).

The booking row lock serialises them. Whichever runs first, the final pair of states must be one of
the valid pairs below, and never a mixture such as a paid booking that is still pending.
"""

import json
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.booking import Booking
from app.models.payment import Payment
from app.payments.signature import header_for
from tests.factories import future_slot, make_booking, make_centre_test, make_payment
from tests.support import run_concurrently

Headers = dict[str, str]
ROUNDS = 12

# (pay or webhook status, cancel status, final booking status, final payment statuses)
type Final = tuple[int, int, str, tuple[str, ...]]


def final_state(db_session: Session, booking_id: Any) -> tuple[str, tuple[str, ...]]:
    db_session.expire_all()
    booking = db_session.get(Booking, booking_id)
    assert booking is not None
    payments = db_session.scalars(
        select(Payment).where(Payment.booking_id == booking_id).order_by(Payment.created_at)
    )
    return booking.status.value, tuple(payment.status.value for payment in payments)


def race_pay_and_cancel(
    client: TestClient, headers: Headers, db_session: Session, booking: Booking, mode: str
) -> Final:
    def pay() -> Response:
        body = {"booking_id": str(booking.id), "simulate": mode}
        return client.post("/payments/", json=body, headers=headers)

    def cancel() -> Response:
        return client.post(f"/bookings/{booking.id}/cancel", headers=headers)

    pay_response, cancel_response = run_concurrently([pay, cancel])
    return (
        pay_response.status_code,
        cancel_response.status_code,
        *final_state(db_session, booking.id),
    )


# Success mode: cancel first, or pay first and then cancel the confirmed booking.
VALID_WITH_SUCCESS: set[Final] = {
    (409, 200, "CANCELLED", ()),  # cancel first: the booking is not payable any more
    (201, 200, "CANCELLED", ("SUCCESS",)),  # pay first: the cancelled booking keeps its payment
}
# Pending mode: cancel first, or pay first and the pending payment blocks the cancel.
VALID_WITH_PENDING: set[Final] = {
    (409, 200, "CANCELLED", ()),
    (202, 409, "PENDING", ("PENDING",)),
}


@pytest.mark.concurrency
@pytest.mark.parametrize(
    ("mode", "valid"), [("SUCCESS", VALID_WITH_SUCCESS), ("PENDING", VALID_WITH_PENDING)]
)
def test_pay_against_cancel_always_ends_in_a_valid_pair_of_states(
    client: TestClient,
    patient: Any,
    patient_headers: Headers,
    db_session: Session,
    *,
    mode: str,
    valid: set[Final],
) -> None:
    offering = make_centre_test(db_session)
    seen: set[Final] = set()
    for day in range(ROUNDS):
        booking = make_booking(
            db_session, user=patient, offering=offering, appointment_at=future_slot(2 + day)
        )
        seen.add(race_pay_and_cancel(client, patient_headers, db_session, booking, mode))
    assert seen <= valid, f"invalid final states: {seen - valid}"


# The webhook against cancel, with a pending payment (spec 8.3, "PENDING mode, step by step").
VALID_WEBHOOK_AGAINST_CANCEL: set[Final] = {
    (200, 409, "CONFIRMED", ("SUCCESS",)),  # cancel first: refused, then the webhook confirms
    (200, 200, "CANCELLED", ("SUCCESS",)),  # webhook first: the confirmed booking is cancelled
}


@pytest.mark.concurrency
def test_the_webhook_against_cancel_always_ends_in_a_valid_pair_of_states(
    client: TestClient,
    patient: Any,
    patient_headers: Headers,
    db_session: Session,
    test_settings: Settings,
) -> None:
    secret = test_settings.webhook_secret.get_secret_value()
    offering = make_centre_test(db_session)
    seen: set[Final] = set()
    for day in range(ROUNDS):
        booking = make_booking(
            db_session, user=patient, offering=offering, appointment_at=future_slot(2 + day)
        )
        payment = make_payment(db_session, booking=booking)
        body = json.dumps(
            {
                "event_id": f"evt_race_{day}",
                "payment_id": str(payment.id),
                "status": "SUCCESS",
                "provider_payment_id": f"sim_race_{day}",
            }
        ).encode()

        def webhook(body: bytes = body) -> Response:
            headers = {
                "Content-Type": "application/json",
                "X-Webhook-Signature": header_for(secret, int(time.time()), body),
            }
            return client.post("/payments/webhook/", content=body, headers=headers)

        def cancel(booking: Booking = booking) -> Response:
            return client.post(f"/bookings/{booking.id}/cancel", headers=patient_headers)

        hook, cancelled = run_concurrently([webhook, cancel])
        assert hook.json()["outcome"] == "APPLIED"
        seen.add((hook.status_code, cancelled.status_code, *final_state(db_session, booking.id)))
    assert seen <= VALID_WEBHOOK_AGAINST_CANCEL, f"invalid: {seen - VALID_WEBHOOK_AGAINST_CANCEL}"

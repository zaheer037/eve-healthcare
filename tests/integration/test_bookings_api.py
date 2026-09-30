"""The bookings over HTTP: create (T3.3). List, read and cancel follow in T3.4.

Covers AB1, AB7 and AB29 of the spec.
"""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.booking import Booking, BookingStatus
from app.models.catalog import CentreTest
from tests.factories import (
    future_slot,
    make_booking,
    make_centre,
    make_centre_test,
    make_diagnostic_test,
)
from tests.support import AUTH_LOOKUP, QueryCounter, run_concurrently

Headers = dict[str, str]


def iso(when: datetime) -> str:
    return when.isoformat()


def post_booking(
    client: TestClient,
    headers: Headers | None,
    offering: CentreTest,
    when: datetime | str | None = None,
    **extra: Any,
) -> Response:
    when = when if when is not None else future_slot()
    body = {
        "centre_id": str(offering.centre_id),
        "test_id": str(offering.test_id),
        "appointment_at": iso(when) if isinstance(when, datetime) else when,
        **extra,
    }
    return client.post("/bookings/", json=body, headers=headers)


def count_bookings(db_session: Session) -> int:
    return db_session.scalar(select(func.count()).select_from(Booking)) or 0


# ==== who may book


def test_a_booking_without_a_token_is_401(client: TestClient, db_session: Session) -> None:
    """AB1."""
    offering = make_centre_test(db_session)
    response = post_booking(client, None, offering)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert count_bookings(db_session) == 0


def test_an_admin_can_book_too(
    client: TestClient, admin_headers: Headers, db_session: Session
) -> None:
    assert post_booking(client, admin_headers, make_centre_test(db_session)).status_code == 201


# ==== the happy path


def test_a_booking_is_created_pending_with_the_price_copied_from_the_offering(
    client: TestClient, patient: Any, patient_headers: Headers, db_session: Session
) -> None:
    centre = make_centre(db_session, name="City Lab")
    test = make_diagnostic_test(db_session, code="CBC", name="Complete Blood Count")
    offering = make_centre_test(db_session, centre=centre, test=test, price=Decimal("499"))
    when = datetime(2031, 5, 1, 10, 0, tzinfo=UTC)
    response = post_booking(
        client, patient_headers, offering, when=datetime.now(UTC) + timedelta(days=9)
    )
    body = response.json()
    assert response.status_code == 201
    assert set(body) == {
        "id",
        "user_id",
        "centre",
        "test",
        "appointment_at",
        "amount",
        "currency",
        "status",
        "created_at",
    }
    assert body["user_id"] == str(patient.id)
    assert body["centre"] == {"id": str(centre.id), "name": "City Lab"}
    assert body["test"] == {"id": str(test.id), "name": "Complete Blood Count"}
    assert (body["amount"], body["currency"], body["status"]) == ("499.00", "INR", "PENDING")
    assert body["appointment_at"].endswith("Z")
    assert when  # the value above only documents the wire format
    stored = db_session.get(Booking, uuid.UUID(body["id"]))
    assert stored is not None
    assert stored.status is BookingStatus.PENDING


def test_seconds_are_cut_off_and_the_offset_is_kept_in_the_stored_instant(
    client: TestClient, patient_headers: Headers, db_session: Session
) -> None:
    offering = make_centre_test(db_session)
    start = (datetime.now(UTC) + timedelta(days=9)).replace(
        hour=10, minute=7, second=0, microsecond=0
    )
    sent = start.astimezone().replace(second=45, microsecond=123456).isoformat()
    response = post_booking(client, patient_headers, offering, when=sent)
    assert response.status_code == 201
    stored = db_session.get(Booking, uuid.UUID(response.json()["id"]))
    assert stored is not None
    assert stored.appointment_at == start


def test_a_later_price_change_does_not_touch_the_booking(
    client: TestClient, admin_headers: Headers, patient_headers: Headers, db_session: Session
) -> None:
    offering = make_centre_test(db_session, price=Decimal("300"))
    booking_id = post_booking(client, patient_headers, offering).json()["id"]
    changed = client.put(
        f"/centres/{offering.centre_id}/tests/{offering.test_id}",
        json={"price": "900"},
        headers=admin_headers,
    )
    assert changed.status_code == 200
    stored = db_session.get(Booking, uuid.UUID(booking_id))
    assert stored is not None
    db_session.refresh(stored)
    assert stored.amount == Decimal("300.00")


# ==== input that the server owns or the schema refuses


@pytest.mark.parametrize(
    ("field", "value"),
    [("amount", 1), ("status", "CONFIRMED"), ("user_id", str(uuid.uuid4())), ("currency", "USD")],
)
def test_a_client_cannot_set_what_the_server_owns(
    client: TestClient,
    patient_headers: Headers,
    db_session: Session,
    field: str,
    value: Any,
) -> None:
    """AB7."""
    offering = make_centre_test(db_session)
    response = post_booking(client, patient_headers, offering, **{field: value})
    assert response.status_code == 422
    assert [item["field"] for item in response.json()["errors"]] == [f"body.{field}"]
    assert count_bookings(db_session) == 0


@pytest.mark.parametrize("when", ["2031-05-01T10:00:00", "tomorrow", "", "2031-05-01"])
def test_a_naive_or_unreadable_datetime_is_422(
    client: TestClient, patient_headers: Headers, db_session: Session, when: str
) -> None:
    offering = make_centre_test(db_session)
    response = post_booking(client, patient_headers, offering, when=when)
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


def test_malformed_ids_are_422_and_name_the_field(
    client: TestClient, patient_headers: Headers
) -> None:
    response = client.post(
        "/bookings/",
        json={"centre_id": "x", "test_id": "y", "appointment_at": iso(future_slot())},
        headers=patient_headers,
    )
    assert sorted(item["field"] for item in response.json()["errors"]) == [
        "body.centre_id",
        "body.test_id",
    ]


def test_the_body_is_checked_after_the_token(client: TestClient) -> None:
    assert client.post("/bookings/", json={}).status_code == 401


# ==== the appointment window


@pytest.mark.parametrize(
    "when",
    [
        lambda: datetime.now(UTC) - timedelta(days=1),
        lambda: datetime.now(UTC) + timedelta(minutes=5),
        lambda: datetime.now(UTC) + timedelta(days=366),
        lambda: "9999-12-31T23:59:59-14:00",
        lambda: "0001-01-01T00:00:00+14:00",
    ],
)
def test_an_appointment_outside_the_window_is_422_with_its_own_code(
    client: TestClient,
    patient_headers: Headers,
    db_session: Session,
    when: Callable[[], datetime | str],
) -> None:
    offering = make_centre_test(db_session)
    response = post_booking(client, patient_headers, offering, when=when())
    assert response.status_code == 422
    assert response.json()["code"] == "APPOINTMENT_OUT_OF_WINDOW"
    assert count_bookings(db_session) == 0


# ==== the offering must be bookable


def test_an_offering_that_cannot_be_booked_is_404_centre_test_not_found(
    client: TestClient, patient_headers: Headers, db_session: Session
) -> None:
    good = make_centre_test(db_session)
    others = [
        make_centre_test(db_session, is_available=False),
        make_centre_test(db_session, centre=make_centre(db_session, is_active=False)),
        make_centre_test(db_session, test=make_diagnostic_test(db_session, is_active=False)),
    ]
    for offering in others:
        response = post_booking(client, patient_headers, offering)
        assert (response.status_code, response.json()["code"]) == (404, "CENTRE_TEST_NOT_FOUND")
    unknown = client.post(
        "/bookings/",
        json={
            "centre_id": str(uuid.uuid4()),
            "test_id": str(good.test_id),
            "appointment_at": iso(future_slot()),
        },
        headers=patient_headers,
    )
    assert unknown.json()["code"] == "CENTRE_TEST_NOT_FOUND"
    not_offered = client.post(
        "/bookings/",
        json={
            "centre_id": str(good.centre_id),
            "test_id": str(make_diagnostic_test(db_session).id),
            "appointment_at": iso(future_slot()),
        },
        headers=patient_headers,
    )
    assert not_offered.status_code == 404
    assert count_bookings(db_session) == 0


# ==== duplicates and the cap


def test_the_same_slot_twice_is_409_duplicate_booking(
    client: TestClient, patient_headers: Headers, db_session: Session
) -> None:
    offering = make_centre_test(db_session)
    when = future_slot()
    assert post_booking(client, patient_headers, offering, when).status_code == 201
    response = post_booking(client, patient_headers, offering, when)
    assert (response.status_code, response.json()["code"]) == (409, "DUPLICATE_BOOKING")
    assert count_bookings(db_session) == 1


def test_the_same_minute_written_with_other_seconds_is_still_a_duplicate(
    client: TestClient, patient_headers: Headers, db_session: Session
) -> None:
    offering = make_centre_test(db_session)
    start = future_slot()
    assert post_booking(client, patient_headers, offering, start.isoformat()).status_code == 201
    later_in_the_minute = (start + timedelta(seconds=40)).isoformat()
    assert post_booking(client, patient_headers, offering, later_in_the_minute).status_code == 409


def test_the_21st_active_booking_is_409_booking_limit_reached(
    client: TestClient, patient: Any, patient_headers: Headers, db_session: Session
) -> None:
    """AB29."""
    offering = make_centre_test(db_session)
    for day in range(20):
        make_booking(
            db_session,
            user=patient,
            offering=offering,
            appointment_at=future_slot(days=2 + day),
        )
    response = post_booking(client, patient_headers, offering, future_slot(days=60))
    assert (response.status_code, response.json()["code"]) == (409, "BOOKING_LIMIT_REACHED")
    assert count_bookings(db_session) == 20


@pytest.mark.concurrency
def test_a_race_of_identical_requests_creates_one_booking(
    client: TestClient, patient_headers: Headers, db_session: Session
) -> None:
    offering = make_centre_test(db_session)
    when = future_slot()
    calls = [
        lambda: post_booking(client, patient_headers, offering, when).status_code for _ in range(8)
    ]
    assert sorted(run_concurrently(calls)) == [201] + [409] * 7
    assert count_bookings(db_session) == 1


# ==== the statement budget


def test_a_booking_takes_three_statements_after_the_authentication_lookup(
    app: FastAPI, client: TestClient, patient_headers: Headers, db_session: Session
) -> None:
    offering = make_centre_test(db_session)
    with QueryCounter(app.state.engine, ignore=(AUTH_LOOKUP,)) as counter:
        response = post_booking(client, patient_headers, offering)
    assert response.status_code == 201
    assert counter.count == 3, counter.statements

"""The bookings over HTTP: create, list, read and cancel.

Covers AB1, AB3, AB7, AB27 and AB29 of the architecture document (§7.4).
"""

import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.models.booking import Booking, BookingStatus
from app.models.catalog import CentreTest
from app.models.payment import PaymentStatus
from tests.factories import (
    future_slot,
    make_booking,
    make_centre,
    make_centre_test,
    make_diagnostic_test,
    make_payment,
    make_user,
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


def test_the_create_response_shows_the_instant_in_utc_like_every_read(
    client: TestClient, patient_headers: Headers, db_session: Session
) -> None:
    """A client that sends `+05:30` gets the same instant back with a `Z`, as a later read does."""
    offering = make_centre_test(db_session)
    start = (datetime.now(UTC) + timedelta(days=9)).replace(hour=10, minute=7, second=0)
    sent = start.astimezone(timezone(timedelta(hours=5, minutes=30))).replace(second=45)
    created = post_booking(client, patient_headers, offering, when=sent.isoformat())
    assert created.status_code == 201
    shown = created.json()["appointment_at"]
    read = client.get(f"/bookings/{created.json()['id']}", headers=patient_headers).json()
    assert shown.endswith("Z")
    assert shown == read["appointment_at"]


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


# ==== Location (T3.4)


def test_a_new_booking_answers_with_a_location_that_leads_to_it(
    client: TestClient, patient_headers: Headers, db_session: Session
) -> None:
    created = post_booking(client, patient_headers, make_centre_test(db_session))
    location = created.headers["location"]
    assert location == f"/bookings/{created.json()['id']}"
    assert client.get(location, headers=patient_headers).json()["id"] == created.json()["id"]


def test_the_location_is_a_path_and_ignores_a_hostile_host_header(
    client: TestClient, patient_headers: Headers, db_session: Session
) -> None:
    offering = make_centre_test(db_session)
    body = {
        "centre_id": str(offering.centre_id),
        "test_id": str(offering.test_id),
        "appointment_at": iso(future_slot()),
    }
    response = client.post(
        "/bookings/", json=body, headers={**patient_headers, "Host": "evil.example"}
    )
    assert response.headers["location"].startswith("/bookings/")
    assert "evil.example" not in response.headers["location"]


# ==== list


def test_the_list_needs_a_token(client: TestClient) -> None:
    response = client.get("/bookings/")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_an_empty_list_is_a_page_with_a_zero_total(
    client: TestClient, patient_headers: Headers
) -> None:
    body = client.get("/bookings/", headers=patient_headers).json()
    assert body == {"data": [], "pagination": {"total": 0, "limit": 20, "offset": 0}}


def test_the_list_holds_only_my_bookings_newest_first(
    client: TestClient, patient: Any, patient_headers: Headers, db_session: Session
) -> None:
    offering = make_centre_test(db_session)
    older = make_booking(db_session, user=patient, offering=offering, appointment_at=future_slot(3))
    newer = make_booking(db_session, user=patient, offering=offering, appointment_at=future_slot(4))
    make_booking(db_session, user=make_user(db_session), offering=offering)  # someone else's
    body = client.get("/bookings/", headers=patient_headers).json()
    assert [item["id"] for item in body["data"]] == [str(newer.id), str(older.id)]
    assert body["pagination"] == {"total": 2, "limit": 20, "offset": 0}
    assert {"centre", "test", "amount", "status"} <= set(body["data"][0])
    assert "payments" not in body["data"][0]  # the list is the short form


def test_the_list_can_be_filtered_by_status_and_the_total_follows(
    client: TestClient, patient: Any, patient_headers: Headers, db_session: Session
) -> None:
    offering = make_centre_test(db_session)
    confirmed = make_booking(
        db_session,
        user=patient,
        offering=offering,
        status=BookingStatus.CONFIRMED,
        appointment_at=future_slot(3),
    )
    for day, status in ((4, BookingStatus.CANCELLED), (5, BookingStatus.FAILED)):
        make_booking(
            db_session,
            user=patient,
            offering=offering,
            status=status,
            appointment_at=future_slot(day),
        )
    filtered = client.get("/bookings/", params={"status": "CONFIRMED"}, headers=patient_headers)
    assert [item["id"] for item in filtered.json()["data"]] == [str(confirmed.id)]
    assert filtered.json()["pagination"]["total"] == 1
    assert client.get("/bookings/", headers=patient_headers).json()["pagination"]["total"] == 3


@pytest.mark.parametrize("status", ["DONE", "confirmed", ""])
def test_a_status_outside_the_four_is_422_and_names_the_field(
    client: TestClient, patient_headers: Headers, status: str
) -> None:
    response = client.get("/bookings/", params={"status": status}, headers=patient_headers)
    assert response.status_code == 422
    assert [item["field"] for item in response.json()["errors"]] == ["query.status"]


def test_the_list_pages(
    client: TestClient, patient: Any, patient_headers: Headers, db_session: Session
) -> None:
    offering = make_centre_test(db_session)
    for day in range(5):
        make_booking(
            db_session, user=patient, offering=offering, appointment_at=future_slot(2 + day)
        )
    first = client.get("/bookings/", params={"limit": 2}, headers=patient_headers).json()
    last = client.get(
        "/bookings/", params={"limit": 2, "offset": 4}, headers=patient_headers
    ).json()
    assert len(first["data"]) == 2
    assert len(last["data"]) == 1
    assert first["pagination"] == {"total": 5, "limit": 2, "offset": 0}
    assert last["pagination"] == {"total": 5, "limit": 2, "offset": 4}


@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({"limit": 0}, "query.limit"),
        ({"limit": 101}, "query.limit"),
        ({"offset": -1}, "query.offset"),
        ({"offset": 10001}, "query.offset"),
    ],
)
def test_bad_paging_is_a_422_that_names_the_field(
    client: TestClient, patient_headers: Headers, params: dict[str, int], field: str
) -> None:
    response = client.get("/bookings/", params=params, headers=patient_headers)
    assert response.status_code == 422
    assert [item["field"] for item in response.json()["errors"]] == [field]


# ==== read one


def test_a_booking_is_read_with_its_payments(
    client: TestClient, patient: Any, patient_headers: Headers, db_session: Session
) -> None:
    booking = make_booking(db_session, user=patient)
    failed = make_payment(
        db_session, booking=booking, status=PaymentStatus.FAILED, failure_reason="declined"
    )
    pending = make_payment(db_session, booking=booking, status=PaymentStatus.PENDING)
    response = client.get(f"/bookings/{booking.id}", headers=patient_headers)
    body = response.json()
    assert response.status_code == 200
    assert body["id"] == str(booking.id)
    assert [payment["id"] for payment in body["payments"]] == [str(failed.id), str(pending.id)]
    assert set(body["payments"][0]) == {
        "id",
        "amount",
        "currency",
        "status",
        "failure_reason",
        "settled_at",
    }
    assert body["payments"][0]["failure_reason"] == "declined"
    assert body["payments"][0]["settled_at"].endswith("Z")
    assert body["payments"][1]["settled_at"] is None


def test_a_booking_without_payments_has_an_empty_list(
    client: TestClient, patient: Any, patient_headers: Headers, db_session: Session
) -> None:
    booking = make_booking(db_session, user=patient)
    assert client.get(f"/bookings/{booking.id}", headers=patient_headers).json()["payments"] == []


def test_someone_elses_booking_is_403_and_an_unknown_one_is_404(
    client: TestClient, patient_headers: Headers, db_session: Session
) -> None:
    others = make_booking(db_session, user=make_user(db_session))
    forbidden = client.get(f"/bookings/{others.id}", headers=patient_headers)
    unknown = client.get(f"/bookings/{uuid.uuid4()}", headers=patient_headers)
    assert (forbidden.status_code, forbidden.json()["code"]) == (403, "FORBIDDEN")
    assert (unknown.status_code, unknown.json()["code"]) == (404, "BOOKING_NOT_FOUND")


def test_an_admin_does_not_see_other_peoples_bookings_either(
    client: TestClient, admin_headers: Headers, db_session: Session
) -> None:
    booking = make_booking(db_session, user=make_user(db_session))
    assert client.get(f"/bookings/{booking.id}", headers=admin_headers).status_code == 403


def test_reading_needs_a_token_and_a_well_formed_id(
    client: TestClient, patient_headers: Headers
) -> None:
    assert client.get(f"/bookings/{uuid.uuid4()}").status_code == 401
    assert client.get("/bookings/not-a-uuid", headers=patient_headers).status_code == 422


# ==== cancel


def cancel_url(booking: Booking) -> str:
    return f"/bookings/{booking.id}/cancel"


@pytest.mark.parametrize("before", [BookingStatus.PENDING, BookingStatus.CONFIRMED])
def test_a_pending_or_confirmed_booking_is_cancelled(
    client: TestClient,
    patient: Any,
    patient_headers: Headers,
    db_session: Session,
    before: BookingStatus,
) -> None:
    booking = make_booking(db_session, user=patient, status=before)
    response = client.post(cancel_url(booking), headers=patient_headers)
    assert response.status_code == 200
    assert response.json()["status"] == "CANCELLED"
    assert response.json()["centre"]["name"]  # the response has the same shape as a read
    db_session.expire_all()
    assert db_session.get(Booking, booking.id).status is BookingStatus.CANCELLED  # type: ignore[union-attr]


def test_a_second_cancel_is_200_and_changes_nothing(
    client: TestClient, patient: Any, patient_headers: Headers, db_session: Session
) -> None:
    booking = make_booking(db_session, user=patient)
    first = client.post(cancel_url(booking), headers=patient_headers)
    second = client.post(cancel_url(booking), headers=patient_headers)
    assert (first.status_code, second.status_code) == (200, 200)
    assert first.json() == second.json()


def test_a_failed_booking_cannot_be_cancelled(
    client: TestClient, patient: Any, patient_headers: Headers, db_session: Session
) -> None:
    booking = make_booking(db_session, user=patient, status=BookingStatus.FAILED)
    response = client.post(cancel_url(booking), headers=patient_headers)
    assert (response.status_code, response.json()["code"]) == (409, "INVALID_BOOKING_STATUS")


def test_a_live_pending_payment_blocks_the_cancel(
    client: TestClient, patient: Any, patient_headers: Headers, db_session: Session
) -> None:
    booking = make_booking(db_session, user=patient)
    make_payment(db_session, booking=booking, status=PaymentStatus.PENDING)
    response = client.post(cancel_url(booking), headers=patient_headers)
    assert (response.status_code, response.json()["code"]) == (409, "PAYMENT_IN_PROGRESS")
    db_session.expire_all()
    assert db_session.get(Booking, booking.id).status is BookingStatus.PENDING  # type: ignore[union-attr]


def test_user_b_cannot_cancel_user_as_booking(
    client: TestClient, patient_headers: Headers, db_session: Session
) -> None:
    """AB3."""
    booking = make_booking(db_session, user=make_user(db_session))
    response = client.post(cancel_url(booking), headers=patient_headers)
    assert (response.status_code, response.json()["code"]) == (403, "FORBIDDEN")
    db_session.expire_all()
    assert db_session.get(Booking, booking.id).status is BookingStatus.PENDING  # type: ignore[union-attr]


def test_cancelling_an_unknown_or_malformed_id_and_without_a_token(
    client: TestClient, patient_headers: Headers
) -> None:
    unknown = client.post(f"/bookings/{uuid.uuid4()}/cancel", headers=patient_headers)
    assert (unknown.status_code, unknown.json()["code"]) == (404, "BOOKING_NOT_FOUND")
    assert client.post("/bookings/nope/cancel", headers=patient_headers).status_code == 422
    assert client.post(f"/bookings/{uuid.uuid4()}/cancel").status_code == 401


def test_cancel_is_not_a_patch_a_client_cannot_write_a_status(
    client: TestClient, patient: Any, patient_headers: Headers, db_session: Session
) -> None:
    booking = make_booking(db_session, user=patient)
    response = client.patch(
        f"/bookings/{booking.id}", json={"status": "CONFIRMED"}, headers=patient_headers
    )
    assert response.status_code == 405
    assert "PATCH" not in response.headers["allow"]


# ==== the row lock (architecture §2.2)


def test_a_stranger_is_told_403_at_once_while_the_owner_holds_the_row_lock(
    client: TestClient, db_engine: Engine, patient_headers: Headers, db_session: Session
) -> None:
    """AB27: the owner predicate is in the lock query, so a stranger never waits behind the owner.
    Waiting would end in a 503 after the 2-second lock timeout."""
    booking = make_booking(db_session, user=make_user(db_session))
    with Session(db_engine) as owner_transaction:
        owner_transaction.execute(
            select(Booking.id).where(Booking.id == booking.id).with_for_update(key_share=True)
        )
        started = time.perf_counter()
        response = client.post(cancel_url(booking), headers=patient_headers)
        elapsed = time.perf_counter() - started
    assert response.status_code == 403
    assert elapsed < 1.0


def test_the_owner_waits_for_the_row_lock_and_then_cancels(
    client: TestClient,
    db_engine: Engine,
    patient: Any,
    patient_headers: Headers,
    db_session: Session,
) -> None:
    booking = make_booking(db_session, user=patient)
    answers: list[Response] = []
    with Session(db_engine) as holder:
        holder.execute(
            select(Booking.id).where(Booking.id == booking.id).with_for_update(key_share=True)
        )
        caller = threading.Thread(
            target=lambda: answers.append(client.post(cancel_url(booking), headers=patient_headers))
        )
        started = time.perf_counter()
        caller.start()
        time.sleep(0.3)  # the request is now waiting for the row
        assert answers == []
        holder.rollback()  # release the lock
        caller.join(timeout=5)
    assert [response.status_code for response in answers] == [200]
    assert time.perf_counter() - started >= 0.3


@pytest.mark.concurrency
def test_ten_cancels_at_once_all_answer_200_and_the_booking_ends_cancelled(
    client: TestClient, patient: Any, patient_headers: Headers, db_session: Session
) -> None:
    booking = make_booking(db_session, user=patient)
    calls = [
        lambda: client.post(cancel_url(booking), headers=patient_headers).status_code
        for _ in range(10)
    ]
    assert run_concurrently(calls) == [200] * 10
    db_session.expire_all()
    assert db_session.get(Booking, booking.id).status is BookingStatus.CANCELLED  # type: ignore[union-attr]


# ==== statement budgets (architecture §5.2)


def test_the_statement_budgets_hold_over_http(
    app: FastAPI,
    client: TestClient,
    patient: Any,
    patient_headers: Headers,
    db_session: Session,
) -> None:
    booking = make_booking(db_session, user=patient)
    make_payment(db_session, booking=booking, status=PaymentStatus.FAILED)
    stranger_booking = make_booking(db_session, user=make_user(db_session))
    checks = [
        ("GET", "/bookings/", 2),
        ("GET", f"/bookings/{booking.id}", 1),
        ("POST", cancel_url(booking), 4),  # lock, pending payment, UPDATE, names
        ("POST", cancel_url(booking), 2),  # a repeat: lock, names
    ]
    with QueryCounter(app.state.engine, ignore=(AUTH_LOOKUP,)) as counter:
        for method, path, budget in checks:
            counter.statements.clear()
            response = client.request(method, path, headers=patient_headers)
            assert response.status_code == 200, (method, path, response.text)
            assert counter.count == budget, (method, path, counter.count, counter.statements)
        counter.statements.clear()  # an error path that tells 403 from 404 runs one more
        assert (
            client.get(f"/bookings/{stranger_booking.id}", headers=patient_headers).status_code
            == 403
        )
        assert counter.count == 2

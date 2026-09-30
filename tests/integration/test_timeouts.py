"""A database that cannot answer in time is a 503 that asks for a retry (architecture §6.3).

A lock timeout, a statement timeout and an exhausted pool are all "try again", on the read path and
the pay path as well as on the webhook. The provider's or the client's own retry is the right tool,
so nothing here is retried in the process.
"""

import threading
import time
from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from app.models.booking import Booking, BookingStatus
from app.models.payment import Payment
from tests.factories import make_booking, make_centre

Headers = dict[str, str]
ClientFactory = Callable[..., TestClient]


def assert_busy(response: Response) -> None:
    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "SERVICE_UNAVAILABLE"
    assert "detail" in response.json()
    for leak in ("could not obtain lock", "canceling statement", "QueuePool", "relation"):
        assert leak not in response.text  # no database text reaches the client


def test_a_read_that_waits_for_a_table_lock_is_503_and_works_again_afterwards(
    client_with: ClientFactory, db_engine: Engine, db_session: Session
) -> None:
    client = client_with(db_lock_timeout_ms=200)
    make_centre(db_session)
    with Session(db_engine) as rival:
        rival.execute(text("LOCK TABLE centres IN ACCESS EXCLUSIVE MODE"))
        assert_busy(client.get("/centres/"))
    assert client.get("/centres/").status_code == 200


def test_a_read_that_runs_too_long_is_503(
    client_with: ClientFactory, db_engine: Engine, db_session: Session
) -> None:
    """The statement timeout counts the wait for a lock, so a held lock also shows it."""
    client = client_with(db_statement_timeout_ms=150, db_lock_timeout_ms=5000)
    make_centre(db_session)
    started = time.perf_counter()
    with Session(db_engine) as rival:
        rival.execute(text("LOCK TABLE centres IN ACCESS EXCLUSIVE MODE"))
        assert_busy(client.get("/centres/"))
    assert time.perf_counter() - started < 3  # it gave up at the statement timeout


def test_paying_a_booking_that_is_locked_elsewhere_is_503_and_creates_nothing(
    client_with: ClientFactory,
    db_engine: Engine,
    patient: Any,
    patient_headers: Headers,
    db_session: Session,
) -> None:
    client = client_with(db_lock_timeout_ms=200)
    booking = make_booking(db_session, user=patient)
    body = {"booking_id": str(booking.id), "simulate": "SUCCESS"}
    with Session(db_engine) as rival:
        rival.execute(
            select(Booking.id).where(Booking.id == booking.id).with_for_update(key_share=True)
        )
        assert_busy(client.post("/payments/", json=body, headers=patient_headers))
    db_session.expire_all()
    assert db_session.get(Booking, booking.id).status is BookingStatus.PENDING  # type: ignore[union-attr]
    assert db_session.scalars(select(Payment)).all() == []
    assert client.post("/payments/", json=body, headers=patient_headers).status_code == 201


def test_cancelling_a_booking_that_is_locked_elsewhere_is_503(
    client_with: ClientFactory,
    db_engine: Engine,
    patient: Any,
    patient_headers: Headers,
    db_session: Session,
) -> None:
    client = client_with(db_lock_timeout_ms=200)
    booking = make_booking(db_session, user=patient)
    with Session(db_engine) as rival:
        rival.execute(
            select(Booking.id).where(Booking.id == booking.id).with_for_update(key_share=True)
        )
        assert_busy(client.post(f"/bookings/{booking.id}/cancel", headers=patient_headers))


def test_a_request_that_finds_no_free_connection_is_503(
    client_with: ClientFactory,
    db_engine: Engine,
    patient: Any,
    patient_headers: Headers,
    db_session: Session,
) -> None:
    """One connection in the pool. A cancel takes it and waits for a row lock, and a read that
    arrives meanwhile waits for a connection until the pool timeout."""
    client = client_with(
        db_pool_size=1, db_max_overflow=0, db_pool_timeout=1, db_lock_timeout_ms=3000
    )
    booking = make_booking(db_session, user=patient)
    slow: list[Response] = []
    with Session(db_engine) as rival:
        rival.execute(
            select(Booking.id).where(Booking.id == booking.id).with_for_update(key_share=True)
        )
        holder = threading.Thread(
            target=lambda: slow.append(
                client.post(f"/bookings/{booking.id}/cancel", headers=patient_headers)
            )
        )
        holder.start()
        time.sleep(0.4)  # the cancel now holds the only connection, waiting for the row
        started = time.perf_counter()
        response = client.get("/centres/")
        waited = time.perf_counter() - started
        rival.rollback()  # let the cancel finish
        holder.join(timeout=10)
    assert_busy(response)
    assert 0.8 < waited < 2.5  # it waited for the pool timeout, then gave up
    assert [reply.status_code for reply in slow] == [200]

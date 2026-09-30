"""The query budget of every endpoint (architecture §5.2, success criterion SC6).

The budget is the number of SQL statements on the success path after the authentication lookup.
This is the one table of the 24 budgets. Other test modules check the same numbers next to the
behaviour that they test, and this one fails when any endpoint has no budget or exceeds it.
"""

import json
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.payments.signature import header_for
from tests.factories import (
    future_slot,
    make_booking,
    make_centre,
    make_centre_test,
    make_diagnostic_test,
    make_payment,
)
from tests.support import AUTH_LOOKUP, QueryCounter

Headers = dict[str, str]

# (method, path) -> statements on the success path (architecture §5.2)
BUDGETS: dict[tuple[str, str], int] = {
    ("POST", "/auth/signup"): 1,
    ("POST", "/auth/login"): 1,
    ("POST", "/auth/token"): 1,
    ("GET", "/users/me"): 0,
    ("GET", "/centres/"): 2,
    ("POST", "/centres/"): 1,
    ("GET", "/centres/{centre_id}"): 2,
    ("PATCH", "/centres/{centre_id}"): 2,
    ("DELETE", "/centres/{centre_id}"): 1,
    ("GET", "/tests/"): 2,
    ("POST", "/tests/"): 1,
    ("GET", "/tests/{test_id}"): 1,
    ("PATCH", "/tests/{test_id}"): 2,
    ("DELETE", "/tests/{test_id}"): 1,
    ("PUT", "/centres/{centre_id}/tests/{test_id}"): 1,
    ("POST", "/bookings/"): 3,
    ("GET", "/bookings/"): 2,
    ("GET", "/bookings/{booking_id}"): 1,
    ("POST", "/bookings/{booking_id}/cancel"): 4,
    ("POST", "/payments/"): 4,
    ("GET", "/payments/{payment_id}"): 1,
    ("POST", "/payments/webhook/"): 8,
    ("GET", "/health/live"): 0,
    ("GET", "/health/ready"): 1,
}
# The cheaper paths of the same endpoints: a repeat, a pending payment, a no-op, a duplicate.
CHEAPER = {"cancel again": 2, "pay again": 2, "pay pending": 3, "webhook no-op": 6, "duplicate": 1}


def test_every_documented_endpoint_has_a_budget_and_no_budget_is_orphaned(
    client: TestClient,
) -> None:
    document = client.get("/openapi.json").json()
    documented = {
        (method.upper(), path) for path, methods in document["paths"].items() for method in methods
    }
    assert documented == set(BUDGETS)


class Measured:
    """Makes a call, and notes a wrong status or a wrong number of statements.

    It collects every miss and does not stop at the first, so one run names all of them.
    """

    def __init__(self, counter: QueryCounter) -> None:
        self._counter = counter
        self.misses: list[str] = []

    def __call__(self, name: str, budget: int, call: Callable[[], Response]) -> Response:
        self._counter.statements.clear()
        response = call()
        if response.status_code >= 400:
            self.misses.append(f"{name}: status {response.status_code} {response.text[:120]}")
        if self._counter.count != budget:
            self.misses.append(f"{name}: {self._counter.count} statements, budget {budget}")
        return response


@pytest.fixture
def measure(app: FastAPI, client: TestClient) -> Iterator[Measured]:
    """Counts the statements of the app's engine, without the authentication lookup."""
    with QueryCounter(app.state.engine, ignore=(AUTH_LOOKUP,)) as counter:
        yield Measured(counter)


def test_health_and_account_budgets(
    client: TestClient, measure: Measured, patient_headers: Headers
) -> None:
    post, get = client.post, client.get
    credentials = {"email": "budget-user@example.in", "password": "a long enough password 123"}
    form = {"username": credentials["email"], "password": credentials["password"]}
    measure("GET /health/live", BUDGETS["GET", "/health/live"], lambda: get("/health/live"))
    measure("GET /health/ready", BUDGETS["GET", "/health/ready"], lambda: get("/health/ready"))
    measure(
        "POST signup",
        BUDGETS["POST", "/auth/signup"],
        lambda: post("/auth/signup", json=credentials),
    )
    measure(
        "POST login", BUDGETS["POST", "/auth/login"], lambda: post("/auth/login", json=credentials)
    )
    measure("POST token", BUDGETS["POST", "/auth/token"], lambda: post("/auth/token", data=form))
    measure(
        "GET /users/me",
        BUDGETS["GET", "/users/me"],
        lambda: get("/users/me", headers=patient_headers),
    )
    assert measure.misses == []


def test_catalog_budgets(
    client: TestClient, measure: Measured, admin_headers: Headers, db_session: Session
) -> None:
    offering = make_centre_test(db_session)
    other_centre, other_test = make_centre(db_session), make_diagnostic_test(db_session)
    get, post, patch, delete = client.get, client.post, client.patch, client.delete
    centre_json = {"name": "Budget Lab", "location": "1 Road"}
    test_json = {"code": "BUDGET", "name": "Budget Test"}
    measure("GET /centres/", BUDGETS["GET", "/centres/"], lambda: get("/centres/"))
    measure(
        "GET /centres/{id}",
        BUDGETS["GET", "/centres/{centre_id}"],
        lambda: get(f"/centres/{offering.centre_id}"),
    )
    measure("GET /tests/", BUDGETS["GET", "/tests/"], lambda: get("/tests/"))
    measure(
        "GET /tests/{id}",
        BUDGETS["GET", "/tests/{test_id}"],
        lambda: get(f"/tests/{offering.test_id}"),
    )
    centre = measure(
        "POST /centres/",
        BUDGETS["POST", "/centres/"],
        lambda: post("/centres/", json=centre_json, headers=admin_headers),
    ).json()["id"]
    measure(
        "PATCH /centres/{id}",
        BUDGETS["PATCH", "/centres/{centre_id}"],
        lambda: patch(f"/centres/{centre}", json={"name": "East"}, headers=admin_headers),
    )
    measure(
        "DELETE /centres/{id}",
        BUDGETS["DELETE", "/centres/{centre_id}"],
        lambda: delete(f"/centres/{centre}", headers=admin_headers),
    )
    test = measure(
        "POST /tests/",
        BUDGETS["POST", "/tests/"],
        lambda: post("/tests/", json=test_json, headers=admin_headers),
    ).json()["id"]
    measure(
        "PATCH /tests/{id}",
        BUDGETS["PATCH", "/tests/{test_id}"],
        lambda: patch(f"/tests/{test}", json={"name": "Two"}, headers=admin_headers),
    )
    measure(
        "DELETE /tests/{id}",
        BUDGETS["DELETE", "/tests/{test_id}"],
        lambda: delete(f"/tests/{test}", headers=admin_headers),
    )
    price = f"/centres/{other_centre.id}/tests/{other_test.id}"
    measure(
        "PUT price",
        BUDGETS["PUT", "/centres/{centre_id}/tests/{test_id}"],
        lambda: client.put(price, json={"price": "150.00"}, headers=admin_headers),
    )
    assert measure.misses == []


def test_booking_budgets(
    client: TestClient,
    measure: Measured,
    patient: Any,
    patient_headers: Headers,
    db_session: Session,
) -> None:
    offering = make_centre_test(db_session)
    to_cancel = make_booking(
        db_session, user=patient, offering=offering, appointment_at=future_slot(5)
    )
    new_booking = {
        "centre_id": str(offering.centre_id),
        "test_id": str(offering.test_id),
        "appointment_at": future_slot(9).isoformat(),
    }
    get, post = client.get, client.post
    cancel = f"/bookings/{to_cancel.id}/cancel"
    measure(
        "POST /bookings/",
        BUDGETS["POST", "/bookings/"],
        lambda: post("/bookings/", json=new_booking, headers=patient_headers),
    )
    measure(
        "GET /bookings/",
        BUDGETS["GET", "/bookings/"],
        lambda: get("/bookings/", headers=patient_headers),
    )
    measure(
        "GET /bookings/{id}",
        BUDGETS["GET", "/bookings/{booking_id}"],
        lambda: get(f"/bookings/{to_cancel.id}", headers=patient_headers),
    )
    measure(
        "POST cancel",
        BUDGETS["POST", "/bookings/{booking_id}/cancel"],
        lambda: post(cancel, headers=patient_headers),
    )
    measure(
        "POST cancel again", CHEAPER["cancel again"], lambda: post(cancel, headers=patient_headers)
    )
    assert measure.misses == []


def test_payment_budgets(
    client: TestClient,
    measure: Measured,
    patient: Any,
    patient_headers: Headers,
    db_session: Session,
) -> None:
    offering = make_centre_test(db_session)
    to_pay = make_booking(
        db_session, user=patient, offering=offering, appointment_at=future_slot(6)
    )
    to_hold = make_booking(
        db_session, user=patient, offering=offering, appointment_at=future_slot(7)
    )
    post = client.post
    pay_now = {"booking_id": str(to_pay.id), "simulate": "SUCCESS"}
    again = {"booking_id": str(to_pay.id)}
    hold = {"booking_id": str(to_hold.id), "simulate": "PENDING"}
    paid = measure(
        "POST /payments/",
        BUDGETS["POST", "/payments/"],
        lambda: post("/payments/", json=pay_now, headers=patient_headers),
    ).json()
    measure(
        "POST /payments/ again",
        CHEAPER["pay again"],
        lambda: post("/payments/", json=again, headers=patient_headers),
    )
    measure(
        "POST /payments/ pending",
        CHEAPER["pay pending"],
        lambda: post("/payments/", json=hold, headers=patient_headers),
    )
    measure(
        "GET /payments/{id}",
        BUDGETS["GET", "/payments/{payment_id}"],
        lambda: client.get(f"/payments/{paid['id']}", headers=patient_headers),
    )
    assert measure.misses == []


def test_webhook_budgets(
    client: TestClient, measure: Measured, test_settings: Settings, db_session: Session
) -> None:
    secret = test_settings.webhook_secret.get_secret_value()
    awaiting = make_payment(db_session, booking=make_booking(db_session))

    def delivery(event_id: str) -> tuple[bytes, Headers]:
        body = {
            "event_id": event_id,
            "payment_id": str(awaiting.id),
            "status": "SUCCESS",
            "provider_payment_id": "sim_budget",
        }
        raw = json.dumps(body, separators=(",", ":")).encode()
        headers = {
            "Content-Type": "application/json",
            "X-Webhook-Signature": header_for(secret, int(time.time()), raw),
        }
        return raw, headers

    first, first_headers = delivery("evt_budget_1")
    second, second_headers = delivery("evt_budget_2")
    hook = "/payments/webhook/"
    measure(
        "POST webhook",
        BUDGETS["POST", hook],
        lambda: client.post(hook, content=first, headers=first_headers),
    )
    measure(
        "POST webhook no-op",
        CHEAPER["webhook no-op"],
        lambda: client.post(hook, content=second, headers=second_headers),
    )
    measure(
        "POST webhook duplicate",
        CHEAPER["duplicate"],
        lambda: client.post(hook, content=first, headers=first_headers),
    )
    assert measure.misses == []


@pytest.mark.parametrize("path", ["/users/me", "/health/live"])
def test_an_endpoint_with_a_budget_of_zero_runs_no_statement_of_its_own(
    app: FastAPI, client: TestClient, patient_headers: Headers, path: str
) -> None:
    with QueryCounter(app.state.engine, ignore=(AUTH_LOOKUP,)) as counter:
        assert client.get(path, headers=patient_headers).status_code == 200
    assert counter.count == 0

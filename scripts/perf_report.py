"""Measure the hot queries and the endpoints at 100,000 bookings (spec section 10.1).

    uv run python -m scripts.seed --scale 100000      # fill a scratch database
    uv run python -m scripts.perf_report explain      # EXPLAIN ANALYZE of the seven hot queries
    uv run fastapi run &                              # the API, on the same database
    uv run python -m scripts.perf_report latency --url http://127.0.0.1:8000

`explain` exits with 1 when a hot query reads `bookings`, `payments` or `webhook_events` with a
sequential scan. Run both commands against a scratch database, never one that holds real data.
"""

import argparse
import json
import random
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from typing import Any, Self

from sqlalchemy import Engine, create_engine, event, func, select, text
from sqlalchemy.orm import Session

from app.bookings.repository import BookingRepository
from app.catalog.repository import CatalogRepository, CentreFilters
from app.core.config import DatabaseSettings
from app.core.pagination import PageParams
from app.models.booking import ACTIVE_STATUSES, Booking, BookingStatus
from app.models.catalog import Centre, CentreTest
from app.models.payment import Payment
from app.payments.inbox import WebhookInbox
from app.payments.repository import PaymentRepository
from app.payments.schemas import WebhookPayload
from scripts.errors import SeedError
from scripts.seed_load import LOAD_PASSWORD, load_user_email, load_user_id

# The tables that grow with use. The catalog tables hold a few dozen rows, and a sequential scan is
# the cheapest plan for them.
BIG_TABLES = ("bookings", "payments", "webhook_events")
ERROR_STATUS = 400
LOGGED_IN_USERS = 150  # room under the cap of 20 for 1,000 bookings, at one Argon2 hash per login
REQUEST_TIMEOUT_SECONDS = 30
LOGIN_ATTEMPTS = 5


def percentile(values: Sequence[float], p: int) -> float:
    """The nearest-rank percentile: the smallest value with at least `p` percent at or below it."""
    if not values:
        raise ValueError("no values")
    ordered = sorted(values)
    rank = max(1, -(-p * len(ordered) // 100))  # the ceiling of p percent of the count
    return ordered[rank - 1]


@dataclass(frozen=True, slots=True)
class LatencyRow:
    name: str
    requests: int
    errors: int
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float

    @classmethod
    def of(cls, name: str, milliseconds: Sequence[float], *, errors: int = 0) -> Self:
        return cls(
            name,
            len(milliseconds),
            errors,
            percentile(milliseconds, 50),
            percentile(milliseconds, 95),
            percentile(milliseconds, 99),
            max(milliseconds),
        )


@dataclass(frozen=True, slots=True)
class PlanSummary:
    name: str
    execution_ms: float
    planning_ms: float
    indexes: list[str]
    big_table_scans: list[str]
    buffers_hit: int
    buffers_read: int


def _walk(node: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = [node]
    for child in node.get("Plans", []):
        nodes += _walk(child)
    return nodes


def summarise_plan(name: str, explained: list[dict[str, Any]]) -> PlanSummary:
    """Read an `EXPLAIN (FORMAT JSON)` result: times, indexes and any big sequential scan."""
    root = explained[0]
    nodes = _walk(root["Plan"])
    indexes = sorted(
        {node["Index Name"] for node in nodes if "Index Name" in node}
        | {index for node in nodes for index in node.get("Conflict Arbiter Indexes", [])}
    )
    scans = sorted(
        {
            node["Relation Name"]
            for node in nodes
            if node.get("Node Type") == "Seq Scan" and node.get("Relation Name") in BIG_TABLES
        }
    )
    return PlanSummary(
        name=name,
        execution_ms=root["Execution Time"],
        planning_ms=root["Planning Time"],
        indexes=indexes,
        big_table_scans=scans,
        buffers_hit=root["Plan"].get("Shared Hit Blocks", 0),
        buffers_read=root["Plan"].get("Shared Read Blocks", 0),
    )


# ---------------------------------------------------------------- explain


@dataclass(frozen=True, slots=True)
class Sample:
    """Real ids from the database, so each query runs on data that exists."""

    user_id: uuid.UUID
    centre_id: uuid.UUID
    test_id: uuid.UUID
    booking_id: uuid.UUID
    appointment_at: datetime
    city: str


def pick_sample(session: Session) -> Sample:
    busiest = session.execute(
        select(Booking.user_id, func.count().label("held"))
        .group_by(Booking.user_id)
        .order_by(text("held DESC"))
        .limit(1)
    ).first()
    centre = session.scalars(select(Centre).where(Centre.city.is_not(None))).first()
    if busiest is None or centre is None:
        raise SeedError("The database is empty. Run `python -m scripts.seed --scale 100000` first.")
    booking = session.scalars(select(Booking).where(Booking.user_id == busiest.user_id)).first()
    if booking is None:
        raise SeedError("The database has no booking.")
    return Sample(
        user_id=booking.user_id,
        centre_id=booking.centre_id,
        test_id=booking.test_id,
        booking_id=booking.id,
        appointment_at=booking.appointment_at,
        city=centre.city or "",
    )


def hot_queries(sample: Sample) -> dict[str, Callable[[Session], object]]:
    """The seven queries of spec section 10.1, each as the call that the API makes."""

    def duplicate_guard(session: Session) -> object:
        # The insert is refused by the unique index `uq_bookings_active_slot`. This is the
        # equivalent probe: the same columns and the same predicate.
        return session.scalars(
            select(Booking.id).where(
                Booking.user_id == sample.user_id,
                Booking.centre_id == sample.centre_id,
                Booking.test_id == sample.test_id,
                Booking.appointment_at == sample.appointment_at,
                Booking.status.in_(ACTIVE_STATUSES),
            )
        ).all()

    def webhook_gate(session: Session) -> object:
        event_id = f"evt_{uuid.uuid4().hex}"
        payload = WebhookPayload.model_validate(
            {"event_id": event_id, "payment_id": str(uuid.uuid4()), "status": "SUCCESS"}
        )
        return WebhookInbox(session).record("SIMULATOR", payload)

    def booking_list(session: Session) -> object:  # the count, then the page: two statements
        repository = BookingRepository(session)
        return repository.count_for_user(sample.user_id), repository.list_for_user(
            sample.user_id, PageParams()
        )

    def centre_list(session: Session) -> object:
        repository = CatalogRepository(session)
        filters = CentreFilters(city=sample.city)
        return repository.count_centres(filters), repository.list_centres(filters, PageParams())

    def centre_detail(session: Session) -> object:
        repository = CatalogRepository(session)
        return repository.get_centre(sample.centre_id), repository.offered_tests(sample.centre_id)

    return {
        "booking list": booking_list,
        "centre list by city": centre_list,
        "centre detail": centre_detail,
        "duplicate guard": duplicate_guard,
        "active-booking count": lambda s: BookingRepository(s).count_active(
            sample.user_id, datetime.now(UTC)
        ),
        "live-payment lookup": lambda s: PaymentRepository(s).live_payment(sample.booking_id),
        "webhook gate": webhook_gate,
    }


def capture_statements(engine: Engine, call: Callable[[Session], object]) -> list[tuple[str, Any]]:
    """The statements that `call` sends, with their parameters. The session is rolled back."""
    seen: list[tuple[str, Any]] = []

    def record(_c: Any, _k: Any, statement: str, parameters: Any, *_r: Any) -> None:
        seen.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", record)
    try:
        with Session(engine) as session:
            call(session)
            session.rollback()  # the webhook gate inserts a row, and this removes it
    finally:
        event.remove(engine, "before_cursor_execute", record)
    return seen


def explain_queries(engine: Engine) -> list[PlanSummary]:
    with Session(engine) as session:
        sample = pick_sample(session)
    summaries: list[PlanSummary] = []
    for name, call in hot_queries(sample).items():
        statements = capture_statements(engine, call)
        with engine.connect() as connection, connection.begin() as transaction:
            for number, (statement, parameters) in enumerate(statements, start=1):
                plan = connection.exec_driver_sql(
                    f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {statement}", parameters
                ).scalar_one()
                label = name if len(statements) == 1 else f"{name} ({number} of {len(statements)})"
                explained = plan if isinstance(plan, list) else json.loads(plan)
                summaries.append(summarise_plan(label, explained))
            transaction.rollback()  # EXPLAIN ANALYZE runs an INSERT, so nothing must stay
    return summaries


def print_plans(summaries: list[PlanSummary]) -> None:
    print("| Query | Execution ms | Planning ms | Buffers hit / read | Indexes |")
    print("|---|---:|---:|---:|---|")
    for plan in summaries:
        indexes = ", ".join(f"`{name}`" for name in plan.indexes) or "none (a small table)"
        print(
            f"| {plan.name} | {plan.execution_ms:.2f} | {plan.planning_ms:.2f} | "
            f"{plan.buffers_hit} / {plan.buffers_read} | {indexes} |"
        )


# ---------------------------------------------------------------- latency


type Request = tuple[str, str, dict[str, str], bytes | None]  # method, URL, headers, body


def http_call(request: Request) -> tuple[int, float]:
    """One request. Return the status and the time in milliseconds."""
    method, url, headers, body = request
    call = urllib.request.Request(url, data=body, headers=headers, method=method)  # noqa: S310
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(call, timeout=REQUEST_TIMEOUT_SECONDS) as response:  # noqa: S310
            response.read()
            status = response.status
    except urllib.error.HTTPError as error:
        error.read()
        status = error.code
    return status, (time.perf_counter() - started) * 1000


def measure(name: str, requests: int, threads: int, make: Callable[[int], Request]) -> LatencyRow:
    """Send `requests` requests from `threads` threads, and keep the time of each success."""
    times: list[float] = []
    errors = 0
    lock = threading.Lock()

    def one(number: int) -> None:
        nonlocal errors
        status, milliseconds = http_call(make(number))
        with lock:
            if status < ERROR_STATUS:
                times.append(milliseconds)
            else:
                errors += 1

    with ThreadPoolExecutor(threads) as pool:
        list(pool.map(one, range(requests)))
    if not times:
        return LatencyRow(name, 0, errors, 0, 0, 0, 0)
    return LatencyRow.of(name, times, errors=errors)


def login(base: str, email: str) -> dict[str, str]:
    """Log in and return the headers of a signed-in request.

    The per-IP limit on the login endpoints answers 429 with `Retry-After` when a run has used the
    minute's budget, and the script waits as a client should.
    """
    body = json.dumps({"email": email, "password": LOAD_PASSWORD}).encode()
    for _ in range(LOGIN_ATTEMPTS):
        request = urllib.request.Request(  # noqa: S310  # the URL comes from the command line
            f"{base}/auth/login",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:  # noqa: S310
                token = json.loads(response.read())["access_token"]
        except urllib.error.HTTPError as error:
            if error.code != HTTPStatus.TOO_MANY_REQUESTS:
                raise
            time.sleep(int(error.headers.get("Retry-After", "1")))
            continue
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    raise SeedError("The login limit did not lift. Wait a minute and run again.")


def measure_endpoints(base: str, engine: Engine, requests: int, threads: int) -> list[LatencyRow]:
    dice = random.Random(1)  # noqa: S311  # the choice of test data, not a secret
    with Session(engine) as session:
        centres = list(session.scalars(select(Centre.id)))
        city = session.scalars(select(Centre.city).where(Centre.city.is_not(None))).first() or ""
        offerings = session.execute(select(CentreTest.centre_id, CentreTest.test_id)).all()
        unpaid = session.execute(
            select(Booking.id, Booking.user_id)
            .where(
                Booking.status == BookingStatus.PENDING,
                ~select(Payment.id).where(Payment.booking_id == Booking.id).exists(),
                Booking.appointment_at > datetime.now(UTC),
            )
            .limit(requests * 3)
        ).all()
    tokens = [login(base, load_user_email(number)) for number in range(LOGGED_IN_USERS)]
    owner_of = {load_user_id(number): number for number in range(LOGGED_IN_USERS)}
    to_pay = [(booking, owner_of[user]) for booking, user in unpaid if user in owner_of][:requests]

    def create_booking(number: int) -> Request:
        centre_id, test_id = dice.choice(offerings)
        start = datetime.now(UTC) + timedelta(minutes=dice.randint(60, 500_000))
        body = json.dumps(
            {
                "centre_id": str(centre_id),
                "test_id": str(test_id),
                "appointment_at": start.replace(second=0, microsecond=0).isoformat(),
            }
        ).encode()
        return "POST", f"{base}/bookings/", tokens[number % LOGGED_IN_USERS], body

    def pay(number: int) -> Request:
        booking_id, owner = to_pay[number % len(to_pay)]
        body = json.dumps({"booking_id": str(booking_id), "simulate": "SUCCESS"}).encode()
        return "POST", f"{base}/payments/", tokens[owner], body

    return [
        measure(
            "GET /centres/?city=",
            requests,
            threads,
            lambda n: ("GET", f"{base}/centres/?city={city}", {}, None),
        ),
        measure(
            "GET /centres/{id}",
            requests,
            threads,
            lambda n: ("GET", f"{base}/centres/{centres[n % len(centres)]}", {}, None),
        ),
        measure(
            "GET /bookings/",
            requests,
            threads,
            lambda n: ("GET", f"{base}/bookings/", tokens[n % LOGGED_IN_USERS], None),
        ),
        measure("POST /bookings/", requests, threads, create_booking),
        measure("POST /payments/", min(requests, len(to_pay)), threads, pay),
    ]


def print_latencies(rows: list[LatencyRow]) -> None:
    print("| Endpoint | Requests | Errors | p50 ms | p95 ms | p99 ms | max ms |")
    print("|---|---:|---:|---:|---:|---:|---:|")
    for row in rows:
        print(
            f"| {row.name} | {row.requests} | {row.errors} | {row.p50_ms:.1f} | "
            f"{row.p95_ms:.1f} | {row.p99_ms:.1f} | {row.max_ms:.1f} |"
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.perf_report", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("explain", help="EXPLAIN (ANALYZE, BUFFERS) of the seven hot queries")
    latency = commands.add_parser("latency", help="p50, p95 and p99 of five endpoints")
    latency.add_argument("--url", default="http://127.0.0.1:8000")
    latency.add_argument("--requests", type=int, default=1000)
    latency.add_argument("--threads", type=int, default=8)
    args = parser.parse_args(argv)

    engine = create_engine(DatabaseSettings().database_url.get_secret_value())
    try:
        if args.command == "explain":
            summaries = explain_queries(engine)
            print_plans(summaries)
            scans = [plan for plan in summaries if plan.big_table_scans]
            for plan in scans:
                print(f"\nSEQUENTIAL SCAN: {plan.name} reads {', '.join(plan.big_table_scans)}")
            return 1 if scans else 0
        print_latencies(measure_endpoints(args.url, engine, args.requests, args.threads))
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())

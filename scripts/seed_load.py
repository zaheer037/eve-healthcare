"""Bulk bookings for the performance checks: `python -m scripts.seed --scale N`.

It adds N bookings on the seeded catalog, spread over N / 10 users, so each user holds about ten.
The statuses follow a fixed mix, and every booking gets the payment that its status implies. The
users share one password hash that is made once, because hashing 10,000 passwords would take ten
minutes, and they exist for load tests only: the command refuses to run in production.
"""

import random
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.models.booking import ACTIVE_STATUSES, Booking, BookingStatus
from app.models.catalog import CentreTest
from app.models.payment import Payment, PaymentStatus
from app.models.user import User
from scripts.errors import SeedError

# The same name always gives the same id, so a second run finds the users of the first.
LOAD_NAMESPACE = uuid.UUID("6f0f1b52-7c3a-4a58-9d1e-2b6a1c7e9d10")
LOAD_PASSWORD = "load-test-password-not-for-production"  # noqa: S105  # refused in production
BOOKINGS_PER_USER = 10
MIN_LEAD_MINUTES = 31  # the window starts 30 minutes from now
MAX_ADVANCE_MINUTES = 365 * 24 * 60
SECONDS_BETWEEN_BOOKINGS = 20
CHUNK = 2_000  # rows per statement
MAX_DRAWS = 100  # a free slot turns up at once, unless the generator is broken

# Per ten bookings: six confirmed, two pending, one failed and one cancelled (60/20/10/10).
STATUS_MIX = (
    *[BookingStatus.CONFIRMED] * 6,
    *[BookingStatus.PENDING] * 2,
    BookingStatus.FAILED,
    BookingStatus.CANCELLED,
)
PAYMENT_OF = {
    BookingStatus.CONFIRMED: PaymentStatus.SUCCESS,
    BookingStatus.CANCELLED: PaymentStatus.SUCCESS,  # a cancelled booking keeps its payment (A7)
    BookingStatus.FAILED: PaymentStatus.FAILED,
}


def load_user_id(number: int) -> uuid.UUID:
    return uuid.uuid5(LOAD_NAMESPACE, f"load-user-{number}")


def load_user_email(number: int) -> str:
    return f"load-user-{number}@example.in"


@dataclass(frozen=True, slots=True)
class LoadReport:
    users: int
    bookings: int
    payments: int


def _chunks(rows: list[dict[str, Any]]) -> Iterator[list[dict[str, Any]]]:
    for start in range(0, len(rows), CHUNK):
        yield rows[start : start + CHUNK]


def _payment_status(booking_status: BookingStatus, position: int) -> PaymentStatus | None:
    """The payment of a booking, or `None`. Half of the pending bookings have a pending payment."""
    if booking_status is BookingStatus.PENDING:
        return PaymentStatus.PENDING if (position // 10) % 2 == 0 else None
    return PAYMENT_OF[booking_status]


type Slot = tuple[uuid.UUID, uuid.UUID, uuid.UUID, datetime]  # user, centre, test, start


def _add_users(session: Session, user_count: int) -> list[uuid.UUID]:
    """Create the load users that are missing. Return the IDs of all of them."""
    user_ids = [load_user_id(number) for number in range(user_count)]
    password_hash = hash_password(LOAD_PASSWORD)  # once, and not once per user
    users = [
        {"id": user_id, "email": load_user_email(number), "password_hash": password_hash}
        for number, user_id in enumerate(user_ids)
    ]
    for chunk in _chunks(users):
        session.execute(
            pg_insert(User).values(chunk).on_conflict_do_nothing(index_elements=["email"])
        )
    return user_ids


class SlotDrawer:
    """Draws an offering and a start time for each booking, and remembers the active slots.

    A user cannot hold one slot twice while both bookings are active, and the unique index says
    so. Ten bookings per user over 30 offerings and a year of minutes collide in about 3 runs in
    100 at 100,000 bookings. So a draw repeats until the user's slot is free.
    """

    def __init__(self, rng: random.Random, offerings: Sequence[Any], now: datetime) -> None:
        self._rng = rng
        self._offerings = offerings
        self._now = now
        self._taken: set[Slot] = set()

    def draw(self, owner: uuid.UUID, *, active: bool) -> tuple[Any, datetime]:
        for _ in range(MAX_DRAWS):
            offering = self._rng.choice(self._offerings)
            minutes_ahead = self._rng.randint(MIN_LEAD_MINUTES, MAX_ADVANCE_MINUTES - 1)
            starts_at = self._now + timedelta(minutes=minutes_ahead)
            starts_at = starts_at.replace(second=0, microsecond=0)
            slot = (owner, offering[0], offering[1], starts_at)
            if not active:
                return offering, starts_at
            if slot not in self._taken:
                self._taken.add(slot)
                return offering, starts_at
        raise SeedError(f"No free slot turned up in {MAX_DRAWS} draws. Check the random generator.")


def _payment_row(
    booking_id: uuid.UUID,
    price: Decimal,
    currency: str,
    status: PaymentStatus,
    created_at: datetime,
) -> dict[str, Any]:
    payment_id = uuid.uuid4()
    settled = status is not PaymentStatus.PENDING
    return {
        "id": payment_id,
        "booking_id": booking_id,
        "amount": price,
        "currency": currency,
        "status": status,
        "provider_payment_id": f"sim_{payment_id.hex}" if settled else None,
        "failure_reason": "declined_by_simulator" if status is PaymentStatus.FAILED else None,
        "settled_at": created_at + timedelta(minutes=1) if settled else None,
        "created_at": created_at,
        "updated_at": created_at,
    }


def _build_rows(
    owners: Sequence[uuid.UUID],
    offerings: Sequence[Any],
    *,
    now: datetime,
    rng: random.Random,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """One booking per owner in the list, and the payment that its status implies."""
    booking_rows: list[dict[str, Any]] = []
    payment_rows: list[dict[str, Any]] = []
    slots = SlotDrawer(rng, offerings, now)
    for position, owner in enumerate(owners):
        status = STATUS_MIX[position % len(STATUS_MIX)]
        offering, starts_at = slots.draw(owner, active=status in ACTIVE_STATUSES)
        centre_id, test_id, price, currency = offering
        created_at = now - timedelta(seconds=(len(owners) - position) * SECONDS_BETWEEN_BOOKINGS)
        booking_id = uuid.uuid4()
        booking_rows.append(
            {
                "id": booking_id,
                "user_id": owner,
                "centre_id": centre_id,
                "test_id": test_id,
                "appointment_at": starts_at,
                "amount": Decimal(price),
                "currency": currency,
                "status": status,
                "created_at": created_at,
                "updated_at": created_at,
            }
        )
        payment_status = _payment_status(status, position)
        if payment_status is not None:
            payment_rows.append(
                _payment_row(booking_id, Decimal(price), currency, payment_status, created_at)
            )
    return booking_rows, payment_rows


def add_load(session: Session, bookings: int, *, now: datetime, rng: random.Random) -> LoadReport:
    """Add `bookings` bookings, their payments and the users that hold them. The caller commits."""
    offerings = session.execute(
        select(
            CentreTest.centre_id, CentreTest.test_id, CentreTest.price, CentreTest.currency
        ).where(CentreTest.is_available)
    ).all()
    if not offerings:
        raise SeedError("Seed the catalog first: no centre offers a test yet.")

    user_count = max(1, bookings // BOOKINGS_PER_USER)
    user_ids = _add_users(session, user_count)
    # Each user gets the same share, in a random order, so a user's statuses are not a pattern.
    owners = [user_ids[position % user_count] for position in range(bookings)]
    rng.shuffle(owners)
    booking_rows, payment_rows = _build_rows(owners, offerings, now=now, rng=rng)
    for chunk in _chunks(booking_rows):
        session.execute(insert(Booking), chunk)
    for chunk in _chunks(payment_rows):  # after the bookings, which they refer to
        session.execute(insert(Payment), chunk)
    return LoadReport(users=user_count, bookings=len(booking_rows), payments=len(payment_rows))

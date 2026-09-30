"""`seed --scale N`: bulk bookings for the performance checks (architecture §§3.6 and 8.1)."""

import random
from collections import Counter
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.core.config import Environment, Settings
from app.models.booking import Booking, BookingStatus
from app.models.payment import Payment, PaymentStatus
from app.models.user import User
from scripts.errors import SeedError
from scripts.seed import run, seed
from scripts.seed_load import LoadReport, add_load
from tests.factories import make_centre_test

NOW = datetime(2030, 1, 1, 9, 0, tzinfo=UTC)
SCALE = 200


@pytest.fixture
def seeded(db_engine: Engine, test_settings: Settings) -> Session:
    """A session on a database that holds the sample catalog."""
    settings = test_settings.model_copy(
        update={"admin_email": "admin@example.com", "admin_password": SecretStr("dev-only-pw-123")}
    )
    with Session(db_engine, expire_on_commit=False) as session:
        seed(session, settings)
        yield session


def load(session: Session, bookings: int = SCALE) -> LoadReport:
    report = add_load(session, bookings, now=NOW, rng=random.Random(7))
    session.commit()
    return report


class CollidingRandom(random.Random):
    """Draws the same offering and the same minute for its first `collisions` draws.

    Every booking of a user then wants one slot, which the unique index on active bookings refuses.
    `collisions=None` never stops.
    """

    def __init__(self, seed: int, collisions: int | None) -> None:
        super().__init__(seed)
        self._collisions = collisions

    def _stuck(self) -> bool:
        if self._collisions is None:
            return True
        self._collisions -= 1
        return self._collisions >= 0

    def choice(self, seq):  # type: ignore[no-untyped-def]
        return seq[0] if self._stuck() else super().choice(seq)

    def randint(self, a: int, b: int) -> int:
        return a + 5 if self._stuck() else super().randint(a, b)


def count(session: Session, model: type[Booking] | type[Payment] | type[User]) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


# ==== the shape that the design promises


def test_scale_adds_the_bookings_over_a_tenth_as_many_users(seeded: Session) -> None:
    report = load(seeded)
    assert report.bookings == SCALE
    assert report.users == SCALE // 10
    load_users = seeded.scalar(
        select(func.count()).select_from(User).where(User.email.like("load-user-%"))
    )
    assert load_users == SCALE // 10
    per_user = Counter(seeded.scalars(select(Booking.user_id)))
    assert set(per_user.values()) == {10}  # each user holds ten, well under the cap of 20


def test_the_statuses_follow_the_mix_of_sixty_twenty_ten_and_ten_percent(seeded: Session) -> None:
    load(seeded)
    mix = Counter(seeded.scalars(select(Booking.status)))
    assert mix == {
        BookingStatus.CONFIRMED: 120,
        BookingStatus.PENDING: 40,
        BookingStatus.FAILED: 20,
        BookingStatus.CANCELLED: 20,
    }


def test_each_booking_gets_the_payment_that_its_status_implies(seeded: Session) -> None:
    report = load(seeded)
    payments_by_booking = {
        payment.booking_id: payment for payment in seeded.scalars(select(Payment))
    }
    seen: Counter[tuple[BookingStatus, PaymentStatus | None]] = Counter()
    for booking in seeded.scalars(select(Booking)):
        payment = payments_by_booking.get(booking.id)
        seen[booking.status, payment.status if payment else None] += 1
        if payment is not None:
            assert (payment.amount, payment.currency) == (booking.amount, booking.currency)
    assert seen == {
        (BookingStatus.CONFIRMED, PaymentStatus.SUCCESS): 120,
        (BookingStatus.CANCELLED, PaymentStatus.SUCCESS): 20,  # a cancelled booking keeps it (A7)
        (BookingStatus.FAILED, PaymentStatus.FAILED): 20,
        (BookingStatus.PENDING, PaymentStatus.PENDING): 20,  # about half of the pending ones
        (BookingStatus.PENDING, None): 20,
    }
    assert report.payments == 180


def test_settled_payments_have_a_time_and_a_reference_and_pending_ones_have_neither(
    seeded: Session,
) -> None:
    load(seeded)
    for payment in seeded.scalars(select(Payment)):
        if payment.status is PaymentStatus.PENDING:
            assert (payment.settled_at, payment.provider_payment_id) == (None, None)
        else:
            assert payment.settled_at is not None
            assert payment.provider_payment_id == f"sim_{payment.id.hex}"
    failed = seeded.scalars(select(Payment).where(Payment.status == PaymentStatus.FAILED)).all()
    assert all(payment.failure_reason for payment in failed)


def test_appointments_are_spread_over_the_booking_window_on_the_minute(seeded: Session) -> None:
    load(seeded)
    times = list(seeded.scalars(select(Booking.appointment_at)))
    assert min(times) >= NOW + timedelta(minutes=30)
    assert max(times) <= NOW + timedelta(days=365)
    assert all(when.second == 0 and when.microsecond == 0 for when in times)
    assert max(times) - min(times) > timedelta(days=200)  # spread out, not bunched


def test_the_rows_use_the_catalog_that_the_seed_made(seeded: Session) -> None:
    load(seeded)
    centres = set(seeded.scalars(select(Booking.centre_id).distinct()))
    assert len(centres) > 1  # more than one centre is booked
    assert count(seeded, Booking) == SCALE


def test_created_at_grows_with_the_row_so_the_list_order_looks_real(seeded: Session) -> None:
    load(seeded)
    ordered = seeded.scalars(select(Booking.created_at).order_by(Booking.created_at)).all()
    assert len(set(ordered)) == SCALE  # every row has its own time
    assert ordered[-1] < NOW


# ==== a second run and the guards


def test_a_second_run_adds_more_bookings_and_reuses_the_users(seeded: Session) -> None:
    load(seeded)
    again = add_load(seeded, SCALE, now=NOW, rng=random.Random(8))
    seeded.commit()
    assert again.bookings == SCALE
    assert count(seeded, Booking) == 2 * SCALE
    assert (
        seeded.scalar(select(func.count()).select_from(User).where(User.email.like("load-user-%")))
        == SCALE // 10
    )


def test_a_scale_smaller_than_ten_still_makes_one_user(seeded: Session) -> None:
    assert load(seeded, bookings=7).users == 1


def test_scale_needs_the_catalog(db_engine: Engine) -> None:
    with Session(db_engine) as session, pytest.raises(SeedError, match="catalog"):
        add_load(session, 10, now=NOW, rng=random.Random(1))


def test_scale_runs_on_any_catalog_that_offers_a_test(
    db_engine: Engine, db_session: Session
) -> None:
    make_centre_test(db_session)
    with Session(db_engine, expire_on_commit=False) as session:
        assert add_load(session, 10, now=NOW, rng=random.Random(1)).bookings == 10
        session.rollback()


def test_scale_is_refused_in_production_and_nothing_is_written(
    test_settings: Settings, db_session: Session, capsys: pytest.CaptureFixture[str]
) -> None:
    production = test_settings.model_copy(
        update={
            "environment": Environment.PRODUCTION,
            "admin_email": "ops@hospital.example",
            "admin_password": SecretStr("a strong production password"),
        }
    )
    assert run(production, scale=100) == 1
    assert "production" in capsys.readouterr().out
    assert count(db_session, Booking) == 0
    assert count(db_session, User) == 0  # not even the admin: the refusal came first


# ==== the command


def test_run_with_a_scale_seeds_first_and_then_loads(
    test_settings: Settings, db_session: Session, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = test_settings.model_copy(
        update={"admin_email": "admin@example.com", "admin_password": SecretStr("dev-only-pw-123")}
    )
    assert run(settings, scale=SCALE) == 0
    output = capsys.readouterr().out
    assert '"event": "seed.done"' in output
    assert '"event": "seed.load_done"' in output
    assert count(db_session, Booking) == SCALE


# ==== a slot that two active bookings of one user would share is drawn again


def test_two_draws_of_one_slot_for_one_user_are_redrawn_and_the_load_still_commits(
    seeded: Session,
) -> None:
    """With an unseeded generator about 3 runs in 100 used to fail on the unique index."""
    report = add_load(seeded, 20, now=NOW, rng=CollidingRandom(3, collisions=60))
    seeded.commit()
    assert report.bookings == 20
    assert count(seeded, Booking) == 20


def test_a_generator_that_can_never_find_a_free_slot_stops_with_a_clear_error(
    seeded: Session,
) -> None:
    with pytest.raises(SeedError, match="free slot"):
        add_load(seeded, 10, now=NOW, rng=CollidingRandom(3, collisions=None))

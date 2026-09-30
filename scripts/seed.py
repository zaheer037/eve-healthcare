"""Seed the admin account and a sample catalog. Safe to run again (architecture §3.6).

    uv run python -m scripts.seed      # on a laptop, or `make seed`
    python -m scripts.seed             # in the container, after `alembic upgrade head`

The run inserts what is missing, matched by natural key, and never changes a row that exists. An
edit that an admin made therefore survives the next run.

    uv run python -m scripts.seed --scale 100000   # also 100,000 bookings, for load tests
"""

import argparse
import random
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal

import structlog
from pydantic import ValidationError
from sqlalchemy import literal_column, select, text
from sqlalchemy.dialects.postgresql import Insert, insert
from sqlalchemy.orm import Session

from app.auth.schemas import LoginRequest
from app.core.clock import utcnow
from app.core.config import PLACEHOLDER_PREFIXES, Environment, Settings
from app.core.db import create_db_engine
from app.core.logging import configure_logging
from app.core.security import hash_password
from app.models.catalog import Centre, CentreTest, DiagnosticTest
from app.models.user import Role, User
from scripts.errors import SeedError
from scripts.seed_data import CENTRES, TESTS
from scripts.seed_load import add_load

log = structlog.get_logger()

ADMIN_FULL_NAME = "Administrator"


@dataclass(frozen=True, slots=True)
class SeedReport:
    """What one run inserted. `False` and three zeros mean that the database was already seeded."""

    admin_created: bool
    tests: int
    centres: int
    prices: int


def admin_credentials(settings: Settings) -> tuple[str, str]:
    """The admin's email and password. The login form must accept them, or nobody could sign in.

    Production also refuses a placeholder and a password below `PASSWORD_MIN_LENGTH`
    (architecture §8.3).
    """
    if not settings.admin_email or settings.admin_password is None:
        raise SeedError("Set ADMIN_EMAIL and ADMIN_PASSWORD to create the admin.")
    try:
        login = LoginRequest(email=settings.admin_email, password=settings.admin_password)
    except ValidationError:  # its text would echo the input, so it is not chained
        raise SeedError("ADMIN_EMAIL or ADMIN_PASSWORD would not pass the login form.") from None
    password = login.password.get_secret_value()
    if settings.environment is Environment.PRODUCTION:
        if password.startswith(PLACEHOLDER_PREFIXES):
            raise SeedError("ADMIN_PASSWORD is a placeholder. Choose a real one in production.")
        if len(password) < settings.password_min_length:
            raise SeedError(
                f"ADMIN_PASSWORD needs at least {settings.password_min_length} characters "
                "in production."
            )
    return login.email, password


def ensure_admin(session: Session, email: str, password: str) -> bool:
    """Create the admin and return `True`. An existing admin is left as it is.

    An account with this email that is not an admin stops the run. Someone may have signed up with
    the address first, and the seed must not hand that person the admin role.
    """
    existing = session.scalar(select(User).where(User.email == email))
    if existing is None:
        admin = User(
            email=email,
            password_hash=hash_password(password),
            full_name=ADMIN_FULL_NAME,
            role=Role.ADMIN,
        )
        session.add(admin)
        return True
    if existing.role is not Role.ADMIN:
        raise SeedError(
            "The account for ADMIN_EMAIL exists and is not an admin. "
            "Remove it or choose another ADMIN_EMAIL."
        )
    return False


def count_inserted(session: Session, stmt: Insert) -> int:
    """Run an `ON CONFLICT DO NOTHING` insert. Return how many rows it inserted.

    Only the inserted rows come back from `RETURNING`, so their number is the answer.
    """
    return len(session.execute(stmt.returning(literal_column("1"))).all())


def insert_missing_tests(session: Session) -> int:
    rows = [{"id": uuid.uuid4(), **asdict(test)} for test in TESTS]
    stmt = insert(DiagnosticTest).values(rows)
    stmt = stmt.on_conflict_do_nothing(index_elements=[DiagnosticTest.code])
    return count_inserted(session, stmt)


def insert_missing_centres(session: Session) -> int:
    rows = [
        {"id": uuid.uuid4(), "name": centre.name, "location": centre.location, "city": centre.city}
        for centre in CENTRES
    ]
    stmt = insert(Centre).values(rows)
    stmt = stmt.on_conflict_do_nothing(index_elements=[Centre.name, Centre.location])
    return count_inserted(session, stmt)


def insert_missing_prices(session: Session) -> int:
    """Price each sample test at each sample centre. Rows that exist, edited or not, stay."""
    found_tests = session.execute(
        select(DiagnosticTest.code, DiagnosticTest.id).where(
            DiagnosticTest.code.in_([test.code for test in TESTS])
        )
    )
    test_ids = dict(found_tests.tuples().all())  # not `dict(found_tests)`: a Result has `keys()`
    centre_ids = {
        (name, location): centre_id
        for centre_id, name, location in session.execute(
            select(Centre.id, Centre.name, Centre.location).where(
                Centre.name.in_([centre.name for centre in CENTRES])
            )
        )
    }
    rows = [
        {
            "centre_id": centre_ids[(centre.name, centre.location)],
            "test_id": test_ids[code],
            "price": Decimal(rupees),
        }
        for centre in CENTRES
        for code, rupees in centre.prices.items()
    ]
    stmt = insert(CentreTest).values(rows)
    stmt = stmt.on_conflict_do_nothing(index_elements=[CentreTest.centre_id, CentreTest.test_id])
    return count_inserted(session, stmt)


def seed(session: Session, settings: Settings) -> SeedReport:
    """Insert what is missing, in one transaction. A refusal writes nothing.

    The credentials are checked first, so a bad setting stops the run before the database is read.
    """
    email, password = admin_credentials(settings)
    admin_created = ensure_admin(session, email, password)
    tests = insert_missing_tests(session)
    centres = insert_missing_centres(session)
    prices = insert_missing_prices(session)
    session.commit()
    return SeedReport(admin_created=admin_created, tests=tests, centres=centres, prices=prices)


def run(settings: Settings, *, scale: int = 0) -> int:
    """Seed the database that `settings` points at. Return the exit code.

    With `scale`, the run also adds that many bookings for load tests. The seed goes first, and it
    commits, so the catalog exists before the load reads it.
    """
    configure_logging(settings)
    engine = create_db_engine(settings)
    try:
        if scale and settings.environment is Environment.PRODUCTION:
            raise SeedError("--scale makes load-test data and is refused in production.")
        with Session(engine, expire_on_commit=False) as session:
            report = seed(session, settings)
            log.info("seed.done", **asdict(report))
            if scale:
                loaded = add_load(session, scale, now=utcnow(), rng=random.Random())  # noqa: S311  # test data, not a secret
                session.commit()
                session.execute(text("ANALYZE users, bookings, payments"))  # fresh statistics
                session.commit()
                log.info("seed.load_done", **asdict(loaded))
    except SeedError as error:
        log.error("seed.refused", reason=str(error))
        return 1
    finally:
        engine.dispose()
    return 0


def _count(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return number


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.seed", description="Seed the admin and the sample catalog."
    )
    parser.add_argument(
        "--scale",
        type=_count,
        default=0,
        help="also add N bookings, for load tests (never in production)",
    )
    return run(Settings(), scale=parser.parse_args(argv).scale)


if __name__ == "__main__":
    raise SystemExit(main())

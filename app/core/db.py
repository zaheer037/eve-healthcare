"""The database engine, the session factory and the session dependency."""

import random
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import Engine, create_engine
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.errors import AppError, ServiceUnavailable

IDLE_IN_TRANSACTION_TIMEOUT_MS = 15_000
POOL_RECYCLE_SECONDS = 1800

# SQLSTATE 40001 is a serialization failure and 40P01 is a deadlock. PostgreSQL chose this
# transaction as the victim, and it made no change, so running it again is safe.
TRANSIENT_SQLSTATES = frozenset({"40001", "40P01"})
RETRY_ATTEMPTS = 3
RETRY_PAUSE_SECONDS = (0.005, 0.05)


def connection_options(settings: Settings) -> str:
    """The server settings that every connection starts with (architecture §§6.3 and 8.1).

    A lock timeout ends a wait for a row lock. A statement timeout ends a slow query. Both reach
    the client as SQLSTATE 55P03 or 57014, which the handlers map to a 503.
    """
    server_settings = {
        "timezone": "UTC",
        "statement_timeout": settings.db_statement_timeout_ms,
        "lock_timeout": settings.db_lock_timeout_ms,
        "idle_in_transaction_session_timeout": IDLE_IN_TRANSACTION_TIMEOUT_MS,
    }
    return " ".join(f"-c {name}={value}" for name, value in server_settings.items())


def create_db_engine(settings: Settings) -> Engine:
    return create_engine(
        settings.database_url.get_secret_value(),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
        pool_pre_ping=True,
        pool_recycle=POOL_RECYCLE_SECONDS,
        hide_parameters=True,  # a database error must not echo bound values such as an email
        connect_args={"options": connection_options(settings)},
    )


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    # expire_on_commit=False: a read after a commit runs no hidden query.
    return sessionmaker(engine, expire_on_commit=False)


def violated_constraint(error: DBAPIError) -> str | None:
    """The name of the constraint that a database error names. psycopg puts it in `diag`."""
    name: str | None = getattr(getattr(error.orig, "diag", None), "constraint_name", None)
    return name


def is_transient(error: Exception, retry_on_constraints: frozenset[str] = frozenset()) -> bool:
    """Would running the whole transaction again fix this error?

    A deadlock and a serialization failure would. So does a unique violation on one of the named
    constraints, when a rival transaction took the value between a check and a write
    (architecture §6.3).
    A lock timeout (55P03) is not transient: the wait already took `lock_timeout`, and the caller
    is better off telling its own client to retry.
    """
    if not isinstance(error, DBAPIError):
        return False
    if getattr(error.orig, "sqlstate", None) in TRANSIENT_SQLSTATES:
        return True
    return isinstance(error, IntegrityError) and violated_constraint(error) in retry_on_constraints


def retry_transient[T](
    fn: Callable[[], T],
    *,
    retry_on_constraints: frozenset[str] = frozenset(),
    attempts: int = RETRY_ATTEMPTS,
    pause: Callable[[float], None] = time.sleep,
) -> T:
    """Run `fn`, a whole transaction, again after a transient failure. Give up after `attempts`.

    `fn` must start and end its own transaction, so a retry begins from a clean state. A short
    random pause between attempts lets two rivals part ways. Any other error is raised at once. A
    failure that persists through every attempt becomes a 503, which tells the client to retry
    later, and the database error stays in the chain.
    """
    attempt = 1
    while True:
        try:
            return fn()
        except Exception as error:
            if not is_transient(error, retry_on_constraints):
                raise
            if attempt >= attempts:
                raise ServiceUnavailable("The service is busy. Try again shortly.") from error
        pause(random.uniform(*RETRY_PAUSE_SECONDS))  # noqa: S311  # jitter, not a secret
        attempt += 1


@contextmanager
def transaction(session: Session) -> Iterator[None]:
    """One use case, one transaction: commit when the block ends, roll back when it raises.

    A service method that writes or locks runs inside it. Every lock is then released before
    FastAPI serialises the response, and a refusal never leaves a transaction open
    (architecture §2.2).
    """
    try:
        yield
        session.commit()
    except BaseException:
        session.rollback()
        raise


@contextmanager
def domain_errors(session: Session, by_constraint: Mapping[str, AppError]) -> Iterator[None]:
    """Turn a violated constraint into the domain error that names it.

    The rollback comes first, so the session stays usable (architecture §2.2). A constraint that
    is not in the mapping is a bug or an outage, so it is raised unchanged. Build the errors for
    each call: an exception carries its traceback, so one instance must not serve two requests.
    """
    try:
        yield
    except IntegrityError as error:
        session.rollback()
        constraint = violated_constraint(error)
        if constraint is not None and constraint in by_constraint:
            raise by_constraint[constraint] from error
        raise


def get_session(request: Request) -> Iterator[Session]:
    """One session per request. Closing it rolls back open work and returns the connection."""
    session_factory: sessionmaker[Session] = request.app.state.session_factory
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


# scope="function": the exit code runs when the handler returns, before the response is sent,
# so the connection goes back to the pool even when the client is slow (architecture §2.4, step 11).
SessionDep = Annotated[Session, Depends(get_session, scope="function")]

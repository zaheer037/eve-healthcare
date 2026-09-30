"""The database engine, the session factory and the session dependency."""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import Engine, create_engine
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.errors import AppError

IDLE_IN_TRANSACTION_TIMEOUT_MS = 15_000
POOL_RECYCLE_SECONDS = 1800


def connection_options(settings: Settings) -> str:
    """The server settings that every connection starts with (spec sections 8.3 and 10.1).

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


@contextmanager
def transaction(session: Session) -> Iterator[None]:
    """One use case, one transaction: commit when the block ends, roll back when it raises.

    A service method that writes or locks runs inside it. Every lock is then released before
    FastAPI serialises the response, and a refusal never leaves a transaction open (spec section
    4.2).
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

    The rollback comes first, so the session stays usable (spec section 4.2). A constraint that
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
# so the connection goes back to the pool even when the client is slow (spec section 4.4, step 11).
SessionDep = Annotated[Session, Depends(get_session, scope="function")]

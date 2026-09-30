"""The database engine, the session factory and the session dependency."""

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings

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

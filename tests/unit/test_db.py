"""The engine settings and the session dependency. Nothing here connects to a database."""

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import Request
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.db import connection_options, create_db_engine, get_session


def test_every_connection_starts_with_the_timeouts_and_utc(settings: Settings) -> None:
    assert connection_options(settings) == (
        "-c timezone=UTC -c statement_timeout=5000 -c lock_timeout=2000"
        " -c idle_in_transaction_session_timeout=15000"
    )


def test_the_engine_follows_the_pool_settings(settings: Settings) -> None:
    engine = create_db_engine(settings)
    try:
        assert engine.dialect.name == "postgresql"
        assert engine.dialect.driver == "psycopg"
        assert engine.pool.size() == 10  # type: ignore[attr-defined]
        assert engine.pool.timeout() == 10  # type: ignore[attr-defined]
        assert engine.hide_parameters is True
    finally:
        engine.dispose()


class FakeSession:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def fake_request(session: FakeSession) -> Request:
    state = SimpleNamespace(session_factory=lambda: session)
    return cast(Request, SimpleNamespace(app=SimpleNamespace(state=state)))


def test_the_session_dependency_yields_one_session_and_closes_it() -> None:
    fake = FakeSession()
    dependency: Iterator[Any] = get_session(fake_request(fake))
    yielded = next(dependency)
    assert yielded is fake
    assert not fake.closed
    with pytest.raises(StopIteration):
        next(dependency)
    assert fake.closed


def test_the_session_is_closed_when_the_handler_fails() -> None:
    fake = FakeSession()
    dependency = get_session(fake_request(fake))
    next(dependency)
    with pytest.raises(RuntimeError):
        dependency.throw(RuntimeError("handler failed"))
    assert fake.closed


def test_session_type_is_the_orm_session() -> None:
    assert Session.__name__ == "Session"

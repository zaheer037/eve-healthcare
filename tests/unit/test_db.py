"""The engine settings and the session dependency. Nothing here connects to a database."""

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import Request
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.db import connection_options, create_db_engine, domain_errors, get_session
from app.core.errors import DuplicateCentre, DuplicateDiagnosticTest


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


# ==== domain_errors: a violated constraint becomes the domain error that names it


class RecordingSession:
    """Remembers what the helper asked of the session, and in which order."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def rollback(self) -> None:
        self.calls.append("rollback")


def integrity_error(constraint: str | None) -> IntegrityError:
    diag = SimpleNamespace(constraint_name=constraint)
    return IntegrityError("INSERT ...", {}, cast(BaseException, SimpleNamespace(diag=diag)))


def test_a_named_constraint_becomes_its_domain_error_after_a_rollback() -> None:
    session = RecordingSession()
    duplicate = DuplicateCentre("This centre already exists.")
    original = integrity_error("uq_centres_name_location")
    mapping = {"uq_centres_name_location": duplicate}
    with pytest.raises(DuplicateCentre) as raised, domain_errors(cast(Session, session), mapping):
        raise original
    assert raised.value is duplicate
    assert raised.value.__cause__ is original  # the database error stays in the chain
    assert session.calls == ["rollback"]


def test_the_error_is_chosen_by_the_constraint_that_fired() -> None:
    mapping = {
        "uq_diagnostic_tests_code": DuplicateDiagnosticTest("code"),
        "uq_diagnostic_tests_name": DuplicateDiagnosticTest("name"),
    }
    session = cast(Session, RecordingSession())
    with pytest.raises(DuplicateDiagnosticTest, match="name"), domain_errors(session, mapping):
        raise integrity_error("uq_diagnostic_tests_name")


@pytest.mark.parametrize("constraint", ["ck_centres_something", None])
def test_a_constraint_that_is_not_mapped_is_raised_unchanged_after_a_rollback(
    constraint: str | None,
) -> None:
    session = RecordingSession()
    original = integrity_error(constraint)
    mapping = {"uq_other": DuplicateCentre()}
    with pytest.raises(IntegrityError) as raised, domain_errors(cast(Session, session), mapping):
        raise original
    assert raised.value is original
    assert session.calls == ["rollback"]


def test_other_errors_and_a_clean_block_pass_through_without_a_rollback() -> None:
    session = RecordingSession()
    with domain_errors(cast(Session, session), {}):
        pass
    with pytest.raises(OperationalError), domain_errors(cast(Session, session), {}):
        raise OperationalError("SELECT 1", {}, cast(BaseException, RuntimeError("down")))
    assert session.calls == []

"""The engine settings and the session dependency. Nothing here connects to a database."""

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import Request
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeout
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.db import (
    connection_options,
    create_db_engine,
    domain_errors,
    get_session,
    is_transient,
    retry_transient,
    transaction,
)
from app.core.errors import DuplicateCentre, DuplicateDiagnosticTest, ServiceUnavailable


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

    def commit(self) -> None:
        self.calls.append("commit")


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


# ==== transaction: commit when the block ends, roll back when it raises


def test_a_finished_block_commits_once() -> None:
    session = RecordingSession()
    with transaction(cast(Session, session)):
        session.calls.append("work")
    assert session.calls == ["work", "commit"]


def test_a_failed_block_rolls_back_and_raises_the_same_error() -> None:
    session = RecordingSession()
    failure = RuntimeError("the use case failed")
    with pytest.raises(RuntimeError) as raised, transaction(cast(Session, session)):
        raise failure
    assert raised.value is failure
    assert session.calls == ["rollback"]  # no commit


def test_a_commit_that_fails_is_rolled_back_too() -> None:
    class FailingCommit(RecordingSession):
        def commit(self) -> None:
            self.calls.append("commit")
            raise OperationalError("COMMIT", {}, cast(BaseException, RuntimeError("down")))

    session = FailingCommit()
    with pytest.raises(OperationalError), transaction(cast(Session, session)):
        pass
    assert session.calls == ["commit", "rollback"]


# ==== retry_transient: run a whole transaction again after a failure that a retry can fix


def database_error(
    kind: type[Exception], sqlstate: str, constraint: str | None = None
) -> Exception:
    diag = SimpleNamespace(constraint_name=constraint)
    orig = cast(BaseException, SimpleNamespace(sqlstate=sqlstate, diag=diag))
    return kind("STATEMENT", {}, orig)  # type: ignore[call-arg]


RETRIED = frozenset({"uq_payments_provider_ref"})  # the caller names what is worth a retry
DEADLOCK = database_error(OperationalError, "40P01")
SERIALIZATION = database_error(OperationalError, "40001")
REFERENCE_COLLISION = database_error(IntegrityError, "23505", "uq_payments_provider_ref")


@pytest.mark.parametrize(
    ("error", "transient"),
    [
        (DEADLOCK, True),
        (SERIALIZATION, True),
        (REFERENCE_COLLISION, True),
        (database_error(OperationalError, "55P03"), False),  # a lock timeout: the provider retries
        (database_error(OperationalError, "57014"), False),  # a statement timeout
        (database_error(IntegrityError, "23505", "uq_users_email"), False),  # another constraint
        (database_error(IntegrityError, "23505", None), False),
        (PoolTimeout("pool"), False),
        (RuntimeError("bug"), False),
    ],
)
def test_only_a_deadlock_a_serialization_failure_and_the_reference_collision_are_transient(
    error: Exception, transient: bool
) -> None:
    assert is_transient(error, RETRIED) is transient


def test_a_unique_violation_is_transient_only_where_the_caller_says_so() -> None:
    assert is_transient(REFERENCE_COLLISION, RETRIED) is True
    assert is_transient(REFERENCE_COLLISION) is False


def test_a_success_runs_once_and_never_pauses() -> None:
    pauses: list[float] = []
    calls: list[int] = []
    assert retry_transient(lambda: calls.append(1) or "done", pause=pauses.append) == "done"
    assert (len(calls), pauses) == (1, [])


def test_a_transient_failure_is_run_again_after_a_short_pause() -> None:
    pauses: list[float] = []
    outcomes: list[Exception | str] = [DEADLOCK, REFERENCE_COLLISION, "third time"]

    def attempt() -> str:
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    assert (
        retry_transient(attempt, retry_on_constraints=RETRIED, pause=pauses.append) == "third time"
    )
    assert len(pauses) == 2
    assert all(0 < seconds < 0.1 for seconds in pauses)  # short, and random so rivals part ways


def test_after_three_transient_failures_it_gives_up_with_a_503() -> None:
    calls: list[int] = []
    pauses: list[float] = []

    def always_deadlocks() -> None:
        calls.append(1)
        raise DEADLOCK

    with pytest.raises(ServiceUnavailable) as raised:
        retry_transient(always_deadlocks, pause=pauses.append)
    assert len(calls) == 3
    assert len(pauses) == 2  # no pause after the last attempt
    assert raised.value.__cause__ is DEADLOCK  # the database error stays in the chain
    assert raised.value.status_code == 503


def test_the_number_of_attempts_is_a_parameter() -> None:
    calls: list[int] = []

    def fails() -> None:
        calls.append(1)
        raise SERIALIZATION

    with pytest.raises(ServiceUnavailable):
        retry_transient(fails, attempts=5, pause=lambda _: None)
    assert len(calls) == 5


@pytest.mark.parametrize(
    "error",
    [database_error(OperationalError, "55P03"), RuntimeError("bug"), PoolTimeout("pool")],
)
def test_any_other_error_is_raised_at_once_and_not_retried(error: Exception) -> None:
    calls: list[int] = []
    pauses: list[float] = []

    def fails() -> None:
        calls.append(1)
        raise error

    with pytest.raises(type(error)) as raised:
        retry_transient(fails, pause=pauses.append)
    assert raised.value is error
    assert (len(calls), pauses) == (1, [])

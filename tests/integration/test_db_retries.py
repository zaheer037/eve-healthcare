"""`retry_transient` against a deadlock that PostgreSQL really detects (architecture §6.3).

The unit tests use a fake error with the right SQLSTATE. This one makes two sessions lock two
rows in opposite order, so PostgreSQL itself picks a victim and raises 40P01.
"""

import threading
import uuid
from collections.abc import Callable

import pytest
from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session

from app.core.db import retry_transient, transaction
from app.models.catalog import Centre
from tests.factories import make_centre
from tests.support import run_concurrently


def crossing(
    engine: Engine,
    name: str,
    rows: tuple[uuid.UUID, uuid.UUID],
    meeting: threading.Barrier,
    attempts: dict[str, int],
) -> Callable[[], None]:
    """A transaction that locks the first of `rows`, waits for the other one to hold its own first
    row, and then asks for the second. Only the first round meets: a retry must not wait for a
    partner that has already finished."""
    first, second = rows

    def one_round() -> None:
        attempts[name] += 1
        with Session(engine) as session, transaction(session):
            session.execute(select(Centre.id).where(Centre.id == first).with_for_update())
            if attempts[name] == 1:
                meeting.wait(timeout=5)
            session.execute(select(Centre.id).where(Centre.id == second).with_for_update())
            session.execute(update(Centre).where(Centre.id == first).values(city=name))

    return lambda: retry_transient(one_round)


@pytest.mark.concurrency
def test_a_deadlock_that_postgresql_detects_is_retried_and_both_transactions_finish(
    db_engine: Engine, db_session: Session
) -> None:
    """The two rounds cannot both finish: each holds what the other wants. PostgreSQL aborts one
    after `deadlock_timeout` (one second) with SQLSTATE 40P01. Without the retry, that caller
    would raise. With it, the victim runs again, meets no partner, and commits."""
    a = make_centre(db_session, name="Deadlock A")
    b = make_centre(db_session, name="Deadlock B")
    meeting = threading.Barrier(2)
    attempts = {"one": 0, "two": 0}
    run_concurrently(
        [
            crossing(db_engine, "one", (a.id, b.id), meeting, attempts),
            crossing(db_engine, "two", (b.id, a.id), meeting, attempts),
        ]
    )
    assert sorted(attempts.values()) == [1, 2]  # exactly one victim, and it ran twice
    cities = [
        db_session.scalar(select(Centre.city).where(Centre.id == centre_id))
        for centre_id in (a.id, b.id)
    ]
    assert cities == ["one", "two"]  # both transactions committed their update

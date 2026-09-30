"""Helpers shared by test modules. Fixtures live in conftest.py."""

import json
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from types import TracebackType
from typing import Any, Self

from sqlalchemy import Engine, event, text

from app.core.config import Settings

SettingsFactory = Callable[..., Settings]

# The authentication lookup of every protected request. The query budgets of the architecture
# document leave it out, so `QueryCounter(engine, ignore=(AUTH_LOOKUP,))` counts what the handler
# itself runs.
AUTH_LOOKUP = "WHERE users.id = "


def wait_for_a_lock_waiter(engine: Engine, *, timeout: float = 5.0) -> None:
    """Return when PostgreSQL shows a backend of this database that waits for a lock.

    A test that starts a request behind a held lock must know that the request reached the lock.
    A fixed sleep only guesses, and it guesses wrong on a busy machine.
    """
    deadline = time.monotonic() + timeout
    with engine.connect() as connection:
        while time.monotonic() < deadline:
            waiting = connection.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )
            )
            connection.rollback()  # each poll reads fresh statistics, not the first snapshot
            if waiting:
                return
            time.sleep(0.005)
    raise AssertionError(f"no backend waited for a lock within {timeout} seconds")


def run_concurrently[T](calls: Sequence[Callable[[], T]]) -> list[T]:
    """Run every call in its own thread and return the results in order.

    A barrier releases all threads at the same moment, so a race is a race and not a timing
    accident. There are no sleeps. An exception in a call is raised here, so the test fails
    with its traceback.
    """
    barrier = threading.Barrier(len(calls))

    def released(call: Callable[[], T]) -> Callable[[], T]:
        def run() -> T:
            barrier.wait()
            return call()

        return run

    with ThreadPoolExecutor(len(calls)) as pool:
        futures = [pool.submit(released(call)) for call in calls]
        return [future.result() for future in futures]


class QueryCounter:
    """Count the SQL statements that an engine runs. Use it as a context manager.

    It listens to SQLAlchemy's `before_cursor_execute`. BEGIN and COMMIT are not cursor
    statements, so they do not count. `ignore` skips statements that contain one of its
    strings, for example the authentication lookup that the query budgets leave out.
    """

    def __init__(self, engine: Engine, ignore: Sequence[str] = ()) -> None:
        self._engine = engine
        self._ignore = tuple(ignore)
        self.statements: list[str] = []

    def _record(self, _conn: Any, _cursor: Any, statement: str, *_rest: Any) -> None:
        if not any(part in statement for part in self._ignore):
            self.statements.append(statement)

    def __enter__(self) -> Self:
        self.statements = []
        event.listen(self._engine, "before_cursor_execute", self._record)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        event.remove(self._engine, "before_cursor_execute", self._record)

    @property
    def count(self) -> int:
        return len(self.statements)

    def assert_at_most(self, budget: int) -> None:
        listing = "\n".join(f"  {i}. {s[:110]}" for i, s in enumerate(self.statements, 1))
        assert self.count <= budget, f"{self.count} statements, budget {budget}:\n{listing}"


def _index_names(node: Any) -> set[str]:
    """Every index that a plan (a parsed `EXPLAIN (FORMAT JSON)`) reads or uses to arbitrate."""
    names: set[str] = set()
    if isinstance(node, dict):
        if "Index Name" in node:
            names.add(node["Index Name"])
        names.update(node.get("Conflict Arbiter Indexes", []))
        for value in node.values():
            names |= _index_names(value)
    elif isinstance(node, list):
        for item in node:
            names |= _index_names(item)
    return names


def indexes_used(engine: Engine, action: Callable[[], object]) -> set[str]:
    """Run `action`, then explain each statement that it ran, and return the indexes that show up.

    A table of a few rows is read with a sequential scan even when a fitting index exists, so
    the plans are made with sequential scans off. That shows which indexes the planner can use,
    and this is what the index plan of architecture §3.4 promises. `EXPLAIN` runs nothing, so it
    is safe for an `UPDATE` or an `INSERT` too.
    """
    seen: list[tuple[str, Any]] = []

    def record(_conn: Any, _cursor: Any, statement: str, parameters: Any, *_rest: Any) -> None:
        seen.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", record)
    try:
        action()
    finally:
        event.remove(engine, "before_cursor_execute", record)
    names: set[str] = set()
    with engine.connect() as connection, connection.begin():
        connection.exec_driver_sql("SET LOCAL enable_seqscan = off")
        for statement, parameters in seen:
            plan = connection.exec_driver_sql(f"EXPLAIN (FORMAT JSON) {statement}", parameters)
            raw = plan.scalar_one()
            names |= _index_names(raw if isinstance(raw, list) else json.loads(raw))
    return names

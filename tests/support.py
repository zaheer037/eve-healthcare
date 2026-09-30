"""Helpers shared by test modules. Fixtures live in conftest.py."""

import threading
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from types import TracebackType
from typing import Any, Self

from sqlalchemy import Engine, event

from app.core.config import Settings

SettingsFactory = Callable[..., Settings]

# The authentication lookup of every protected request. The query budgets of the spec leave it out,
# so `QueryCounter(engine, ignore=(AUTH_LOOKUP,))` counts what the handler itself runs.
AUTH_LOOKUP = "WHERE users.id = "


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

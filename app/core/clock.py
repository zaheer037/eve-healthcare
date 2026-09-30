"""The time source. A service takes a `Clock`, so a test can fix the time."""

from collections.abc import Callable
from datetime import UTC, datetime

type Clock = Callable[[], datetime]


def utcnow() -> datetime:
    return datetime.now(UTC)

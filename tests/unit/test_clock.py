from datetime import UTC, datetime, timedelta

from app.core.clock import utcnow


def test_utcnow_is_timezone_aware_and_current() -> None:
    now = utcnow()
    assert now.tzinfo is UTC
    assert abs(datetime.now(UTC) - now) < timedelta(seconds=5)

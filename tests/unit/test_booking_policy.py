"""Booking rules that need no database: the window, the cap and the 403 or 404 choice."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.bookings.policy import check_active_cap, check_appointment_window, owner_error
from app.core.errors import (
    AppointmentOutOfWindow,
    BookingLimitReached,
    BookingNotFound,
    Forbidden,
)

NOW = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
LEAD = timedelta(minutes=30)
ADVANCE = timedelta(days=365)


def check(appointment_at: datetime) -> None:
    check_appointment_window(appointment_at, now=NOW, min_lead=LEAD, max_advance=ADVANCE)


# ==== the appointment window


def test_an_appointment_inside_the_window_is_accepted() -> None:
    check(NOW + timedelta(days=7))


def test_both_ends_of_the_window_are_accepted() -> None:
    check(NOW + LEAD)
    check(NOW + ADVANCE)


@pytest.mark.parametrize(
    "appointment_at",
    [
        NOW + LEAD - timedelta(microseconds=1),  # one tick too soon
        NOW + timedelta(minutes=29),
        NOW,  # it starts now
        NOW - timedelta(days=1),  # in the past
        NOW + ADVANCE + timedelta(microseconds=1),  # one tick too late
        NOW + timedelta(days=366),
    ],
)
def test_an_appointment_outside_the_window_is_refused(appointment_at: datetime) -> None:
    with pytest.raises(AppointmentOutOfWindow):
        check(appointment_at)


def test_the_message_names_the_limits() -> None:
    with pytest.raises(AppointmentOutOfWindow, match=r"30 minutes.*365 days"):
        check(NOW)


def test_the_offset_of_the_datetime_does_not_change_the_answer() -> None:
    india = timezone(timedelta(hours=5, minutes=30))
    same_instant = (NOW + timedelta(days=2)).astimezone(india)
    check(same_instant)
    with pytest.raises(AppointmentOutOfWindow):
        check((NOW + timedelta(minutes=29)).astimezone(india))


@pytest.mark.parametrize(
    "appointment_at",
    [
        datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone(timedelta(hours=-14))),
        datetime(1, 1, 1, 0, 0, 0, tzinfo=timezone(timedelta(hours=14))),
    ],
)
def test_an_extreme_datetime_is_refused_and_never_overflows(appointment_at: datetime) -> None:
    """Converting these to UTC raises OverflowError. The check compares them as they are (sharp
    edge 50)."""
    with pytest.raises(AppointmentOutOfWindow):
        check(appointment_at)


# ==== the cap on active bookings


def test_a_user_under_the_cap_may_book() -> None:
    check_active_cap(active=19, cap=20)


@pytest.mark.parametrize("active", [20, 21, 500])
def test_a_user_at_the_cap_may_not(active: int) -> None:
    with pytest.raises(BookingLimitReached, match="20"):
        check_active_cap(active=active, cap=20)


# ==== who sees what (decision D3)


def test_a_booking_that_exists_but_is_not_yours_is_forbidden() -> None:
    error = owner_error(exists=True, not_found=BookingNotFound())
    assert isinstance(error, Forbidden)


def test_a_booking_that_does_not_exist_is_the_not_found_error_that_the_caller_names() -> None:
    not_found = BookingNotFound("No booking has this ID.")
    assert owner_error(exists=False, not_found=not_found) is not_found

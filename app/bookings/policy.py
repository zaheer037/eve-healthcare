"""Booking rules that need no database (spec sections 5.5 and 7.4). Pure functions."""

from datetime import datetime, timedelta

from app.core.errors import (
    AppError,
    AppointmentOutOfWindow,
    BookingLimitReached,
    Forbidden,
)

SECONDS_PER_MINUTE = 60


def check_appointment_window(
    appointment_at: datetime, *, now: datetime, min_lead: timedelta, max_advance: timedelta
) -> None:
    """Raise `AppointmentOutOfWindow` unless the start lies between `min_lead` and `max_advance`
    from `now`. Both ends are allowed.

    It compares two aware datetimes and converts neither to UTC. Converting the datetime
    `9999-12-31T23:59:59-14:00` raises `OverflowError`, and a comparison does not (sharp edge 50).
    """
    if not now + min_lead <= appointment_at <= now + max_advance:
        minutes = int(min_lead.total_seconds()) // SECONDS_PER_MINUTE
        raise AppointmentOutOfWindow(
            f"The appointment must start between {minutes} minutes and "
            f"{max_advance.days} days from now."
        )


def check_active_cap(*, active: int, cap: int) -> None:
    """Raise `BookingLimitReached` when the user already holds `cap` active future bookings.

    The cap is soft: two simultaneous requests can both pass it (assumption A17).
    """
    if active >= cap:
        raise BookingLimitReached(f"You already hold {cap} active bookings.")


def owner_error(*, exists: bool, not_found: AppError) -> AppError:
    """The error for a booking or a payment that the caller cannot use.

    Decision D3: a row that exists but belongs to someone else gives 403, and a missing row gives
    the `not_found` error. Answering 404 for both is one change, here. Four call sites share this
    function: cancel, pay, and the two `get_owned` reads (spec section 4.2).
    """
    return Forbidden("This resource belongs to another user.") if exists else not_found

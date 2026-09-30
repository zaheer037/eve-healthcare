"""Booking rules that need no database (spec sections 5.5 and 7.4). Pure functions."""

import uuid
from datetime import datetime, timedelta

from app.core.errors import (
    AppError,
    AppointmentOutOfWindow,
    BookingLimitReached,
    BookingNotPayable,
    Forbidden,
)
from app.models.booking import BookingStatus

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


def check_payable(
    *, booking_id: uuid.UUID, status: BookingStatus, appointment_at: datetime, now: datetime
) -> None:
    """Raise `BookingNotPayable` unless the booking is `PENDING` and its appointment lies ahead.

    An appointment that starts exactly now has started (assumption A21). A booking that already
    holds a live payment never gets here: the replay check comes first (spec section 8.1).
    """
    if status is not BookingStatus.PENDING:
        raise BookingNotPayable(
            f"Booking {booking_id} has status {status.value}. Only PENDING bookings can be paid."
        )
    if appointment_at <= now:
        raise BookingNotPayable(f"The appointment of booking {booking_id} has already started.")


def owner_error(*, exists: bool, not_found: AppError) -> AppError:
    """The error for a booking or a payment that the caller cannot use.

    Decision D3: a row that exists but belongs to someone else gives 403, and a missing row gives
    the `not_found` error. Answering 404 for both is one change, here. Four call sites share this
    function: cancel, pay, and the two `get_owned` reads (spec section 4.2).
    """
    return Forbidden("This resource belongs to another user.") if exists else not_found

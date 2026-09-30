"""The booking state machine (architecture §4.1). Pure: no database and no framework.

The table below is data, not a chain of `if` statements, so a reviewer reads every legal move at
a glance and a test walks the whole 4 x 4 grid.
"""

from collections.abc import Mapping

from app.core.errors import InvalidBookingStatus
from app.models.booking import BookingStatus

BOOKING_TRANSITIONS: Mapping[BookingStatus, frozenset[BookingStatus]] = {
    BookingStatus.PENDING: frozenset(
        {BookingStatus.CONFIRMED, BookingStatus.FAILED, BookingStatus.CANCELLED}
    ),
    BookingStatus.CONFIRMED: frozenset({BookingStatus.CANCELLED}),
    BookingStatus.FAILED: frozenset(),
    BookingStatus.CANCELLED: frozenset(),
}


def transition(current: BookingStatus, target: BookingStatus) -> BookingStatus:
    """Return `target` when the move is legal. Otherwise raise `InvalidBookingStatus`.

    A status never moves to itself. A repeated cancel is answered before this is called.
    """
    if target not in BOOKING_TRANSITIONS[current]:
        raise InvalidBookingStatus(f"A {current.value} booking cannot become {target.value}.")
    return target

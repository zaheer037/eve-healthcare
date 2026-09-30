"""The booking state machine: every move in the 4 x 4 grid (spec section 6.1)."""

import itertools

import pytest

from app.bookings.state import BOOKING_TRANSITIONS, transition
from app.core.errors import InvalidBookingStatus
from app.models.booking import BookingStatus

PENDING, CONFIRMED, FAILED, CANCELLED = (
    BookingStatus.PENDING,
    BookingStatus.CONFIRMED,
    BookingStatus.FAILED,
    BookingStatus.CANCELLED,
)

# The five arrows of the state diagram, and nothing else.
LEGAL = {
    (PENDING, CONFIRMED),  # the payment succeeded
    (PENDING, FAILED),  # the payment failed
    (PENDING, CANCELLED),  # the user cancels
    (CONFIRMED, CANCELLED),  # the user cancels
}
GRID = list(itertools.product(BookingStatus, BookingStatus))


def test_the_grid_covers_all_sixteen_moves() -> None:
    assert len(GRID) == 16
    assert set(BOOKING_TRANSITIONS) == set(BookingStatus)  # every status has a row in the table


@pytest.mark.parametrize(("current", "target"), [move for move in GRID if move in LEGAL])
def test_a_legal_move_returns_the_new_status(current: BookingStatus, target: BookingStatus) -> None:
    assert transition(current, target) is target


@pytest.mark.parametrize(("current", "target"), [move for move in GRID if move not in LEGAL])
def test_every_other_move_is_refused(current: BookingStatus, target: BookingStatus) -> None:
    with pytest.raises(InvalidBookingStatus) as refused:
        transition(current, target)
    assert current.value in str(refused.value)
    assert target.value in str(refused.value)


@pytest.mark.parametrize("final", [FAILED, CANCELLED])
def test_a_final_status_has_no_way_out(final: BookingStatus) -> None:
    assert BOOKING_TRANSITIONS[final] == frozenset()


def test_a_status_never_moves_to_itself() -> None:
    """A repeated cancel is answered by the service before it asks the state machine."""
    for status in BookingStatus:
        assert status not in BOOKING_TRANSITIONS[status]

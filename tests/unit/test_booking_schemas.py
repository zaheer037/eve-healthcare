"""Booking request and response models (architecture §5.6)."""

import uuid
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from app.bookings.schemas import BookingCreate, BookingRead
from app.models.booking import BookingStatus

CENTRE_ID = str(uuid.uuid4())
TEST_ID = str(uuid.uuid4())


def parse(**changes: Any) -> BookingCreate:
    body = {"centre_id": CENTRE_ID, "test_id": TEST_ID, "appointment_at": "2030-05-01T10:00:00Z"}
    return BookingCreate.model_validate({**body, **changes})


def refused(**changes: Any) -> set[str]:
    with pytest.raises(ValidationError) as error:
        parse(**changes)
    return {str(item["loc"][0]) for item in error.value.errors()}


def test_a_valid_body_parses() -> None:
    booking = parse()
    assert (str(booking.centre_id), str(booking.test_id)) == (CENTRE_ID, TEST_ID)
    assert booking.appointment_at == datetime(2030, 5, 1, 10, 0, tzinfo=UTC)


def test_seconds_and_microseconds_are_set_to_zero() -> None:
    booking = parse(appointment_at="2030-05-01T10:07:45.123456+05:30")
    assert (booking.appointment_at.second, booking.appointment_at.microsecond) == (0, 0)
    assert booking.appointment_at.minute == 7  # the minute stays
    assert booking.appointment_at.utcoffset() == timedelta(
        hours=5, minutes=30
    )  # so does the offset


@pytest.mark.parametrize(
    "value",
    ["2030-05-01T10:00:00", "2030-05-01", "tomorrow", "", None, 1893456000, "2030-13-01T10:00:00Z"],
)
def test_an_appointment_needs_a_timezone_and_a_real_date(value: Any) -> None:
    assert refused(appointment_at=value) == {"appointment_at"}


def test_the_extreme_datetime_parses_and_is_left_to_the_window_check() -> None:
    booking = parse(appointment_at="9999-12-31T23:59:59-14:00")
    assert booking.appointment_at.year == 9999


@pytest.mark.parametrize("field", ["amount", "status", "user_id", "id", "currency"])
def test_a_field_that_the_server_owns_is_refused(field: str) -> None:
    """AB7: a client never sends the price, the status or the owner."""
    assert refused(**{field: "1"}) == {field}


@pytest.mark.parametrize("field", ["centre_id", "test_id"])
@pytest.mark.parametrize("value", ["not-a-uuid", "", None, 7])
def test_ids_must_be_uuids(field: str, value: Any) -> None:
    assert refused(**{field: value}) == {field}


def test_every_field_is_required() -> None:
    with pytest.raises(ValidationError) as error:
        BookingCreate.model_validate({})
    assert {str(item["loc"][0]) for item in error.value.errors()} == {
        "centre_id",
        "test_id",
        "appointment_at",
    }


def test_a_booking_reads_with_money_as_a_string_and_time_with_an_offset() -> None:
    booking = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        centre=SimpleNamespace(id=uuid.uuid4(), name="City Lab"),
        test=SimpleNamespace(id=uuid.uuid4(), name="Complete Blood Count"),
        appointment_at=datetime(2030, 5, 1, 10, 0, tzinfo=timezone.utc),  # noqa: UP017
        amount=Decimal("499.00"),
        currency="INR",
        status=BookingStatus.PENDING,
        created_at=datetime(2030, 1, 1, 9, 0, tzinfo=UTC),
    )
    body = BookingRead.model_validate(booking).model_dump(mode="json")
    assert body["amount"] == "499.00"
    assert body["appointment_at"] == "2030-05-01T10:00:00Z"
    assert body["status"] == "PENDING"
    assert body["centre"] == {"id": str(booking.centre.id), "name": "City Lab"}
    assert body["test"] == {"id": str(booking.test.id), "name": "Complete Blood Count"}
    assert set(body) == {
        "id",
        "user_id",
        "centre",
        "test",
        "appointment_at",
        "amount",
        "currency",
        "status",
        "created_at",
    }

"""Request and response models of the booking endpoints (architecture §5.6)."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import AfterValidator, AwareDatetime, BaseModel, BeforeValidator, ConfigDict
from pydantic_core import PydanticCustomError

from app.models.booking import BookingStatus
from app.models.payment import PaymentStatus


def _text_or_datetime_only(value: object) -> object:
    """Refuse a number. Pydantic reads `1893456000` as a Unix time, and the API documents only
    ISO 8601 text."""
    if not isinstance(value, str | datetime):
        raise PydanticCustomError(
            "datetime_type", "Input should be a date-time string with a UTC offset"
        )
    return value


def _to_the_minute(value: datetime) -> datetime:
    """Cut off seconds and microseconds, so "10:00:45" and "10:00:00" name one slot."""
    return value.replace(second=0, microsecond=0)


# A naive datetime is refused: a booking needs an instant. The offset is kept as it came, because
# the window check compares aware datetimes and never converts them.
AppointmentAt = Annotated[
    AwareDatetime, BeforeValidator(_text_or_datetime_only), AfterValidator(_to_the_minute)
]


class BookingCreate(BaseModel):
    """What a client may choose. The amount, the status and the owner are the server's."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "centre_id": "5fc13147-109b-4782-9795-1156861d2dd3",
                    "test_id": "d30e9668-8d50-4240-9f83-18312d8e3b25",
                    "appointment_at": "2030-05-01T10:30:00+05:30",
                }
            ]
        },
    )

    centre_id: uuid.UUID
    test_id: uuid.UUID
    appointment_at: AppointmentAt


class CentreRef(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str


class DiagnosticTestRef(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str


class BookingRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    centre: CentreRef
    test: DiagnosticTestRef
    appointment_at: datetime
    amount: Decimal
    currency: str
    status: BookingStatus
    created_at: datetime


class PaymentSummary(BaseModel):
    """A payment as a booking shows it. `PaymentRead` in the payments package has more."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    amount: Decimal
    currency: str
    status: PaymentStatus
    failure_reason: str | None
    settled_at: datetime | None


class BookingDetailRead(BookingRead):
    payments: list[PaymentSummary]

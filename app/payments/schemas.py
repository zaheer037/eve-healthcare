"""Request and response models of the payment endpoints and the webhook (architecture §5.6)."""

import unicodedata
import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from app.bookings.schemas import PaymentSummary
from app.core.schemas import BIDI_CONTROLS, CurrencyCode, Money, SafeText
from app.models.booking import BookingStatus
from app.models.payment import PaymentStatus, WebhookOutcome
from app.payments.settlement import TerminalStatus

EventId = Annotated[SafeText, Field(max_length=128)]
ProviderReference = Annotated[SafeText, Field(max_length=128)]
MAX_FAILURE_REASON = 200


def clean_reason(value: object) -> object:
    """Make the reason fit for the database and the screen. Never refuse it.

    It only explains. A refused event would come again, the provider would retry the same body,
    and the payment would stay pending while a cancel answers 409. So control characters, bidi
    overrides and lone surrogates become spaces, white space collapses, the text is cut at 200
    characters, and a text with nothing left becomes `None`. Something that is not text is still
    a 422.
    """
    if not isinstance(value, str):
        return value
    spaced = "".join(
        " " if unicodedata.category(char) in ("Cc", "Cs") or char in BIDI_CONTROLS else char
        for char in value
    )
    return " ".join(spaced.split())[:MAX_FAILURE_REASON].strip() or None


FailureReason = Annotated[str | None, BeforeValidator(clean_reason)]


class PaymentCreate(BaseModel):
    """Pay a booking. The amount always comes from the booking, so a client never sends one."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {"booking_id": "7c1f0c1e-2a4b-4b58-9d6e-3f1a2b4c5d6e"},
                {"booking_id": "7c1f0c1e-2a4b-4b58-9d6e-3f1a2b4c5d6e", "simulate": "PENDING"},
            ]
        },
    )

    booking_id: uuid.UUID
    simulate: PaymentStatus | None = Field(
        default=None,
        description="Force the result: SUCCESS, FAILED, or PENDING (the webhook settles it later)."
        " Refused when the setting `PAYMENT_ALLOW_SIMULATE_OVERRIDE` is off.",
    )


class BookingRef(BaseModel):
    id: uuid.UUID
    status: BookingStatus


class PaymentRead(PaymentSummary):
    """A payment with its booking's id and current status."""

    booking_id: uuid.UUID
    booking: BookingRef


class WebhookPayload(BaseModel):
    """One event from the provider (architecture §6.5).

    A provider can add fields, so unknown ones are ignored. Only `payment_id` names anything: the
    receiver derives the booking from its own records and never trusts one in the payload.
    """

    model_config = ConfigDict(extra="ignore")

    event_id: EventId
    payment_id: uuid.UUID
    status: TerminalStatus
    provider_payment_id: ProviderReference | None = None
    amount: Money | None = None
    currency: CurrencyCode | None = None
    failure_reason: FailureReason = None


class WebhookAck(BaseModel):
    """The answer to a delivery that was stored or recognised. A business failure is an outcome."""

    status: Literal["processed", "duplicate"]
    outcome: WebhookOutcome | None = None
    detail: str | None = None

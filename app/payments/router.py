"""Payment endpoints (architecture §5.2). Each needs a signed-in user, except the webhook."""

import uuid
from http import HTTPStatus
from typing import Annotated

from fastapi import Depends, Request, Response

from app.auth.dependencies import CurrentUser
from app.core.db import SessionDep
from app.core.errors import (
    BookingNotFound,
    BookingNotPayable,
    Forbidden,
    InvalidToken,
    NotAuthenticated,
    PayloadTooLarge,
    PaymentNotFound,
    SimulationNotAllowed,
    ValidationFailed,
    WebhookSignatureInvalid,
)
from app.core.http import located
from app.core.problem import problems
from app.core.routing import TolerantRouter
from app.models.payment import PaymentStatus
from app.payments.dependencies import verified_webhook_payload
from app.payments.schemas import (
    BookingRef,
    PaymentCreate,
    PaymentRead,
    WebhookAck,
    WebhookPayload,
)
from app.payments.service import PaymentService, PaymentView, PayOutcome
from app.payments.simulator import PaymentSimulator

router = TolerantRouter(prefix="/payments", tags=["payments"])


SIGNED_IN = (NotAuthenticated, InvalidToken)


def get_payment_service(request: Request, session: SessionDep) -> PaymentService:
    settings = request.app.state.settings
    return PaymentService(session, settings, PaymentSimulator(settings.payment_success_rate))


PaymentServiceDep = Annotated[PaymentService, Depends(get_payment_service)]


def payment_read(view: PaymentView) -> PaymentRead:
    payment = view.payment
    return PaymentRead(
        id=payment.id,
        amount=payment.amount,
        currency=payment.currency,
        status=payment.status,
        failure_reason=payment.failure_reason,
        settled_at=payment.settled_at,
        booking_id=payment.booking_id,
        booking=BookingRef(id=payment.booking_id, status=view.booking_status),
    )


def pay_status(outcome: PayOutcome) -> HTTPStatus:
    """`202` while the payment is pending, `201` for a new one that settled, `200` for a replay."""
    if outcome.view.payment.status is PaymentStatus.PENDING:
        return HTTPStatus.ACCEPTED
    return HTTPStatus.CREATED if outcome.created else HTTPStatus.OK


@router.post(
    "/",
    status_code=201,
    summary="Pay a booking",
    responses={
        **problems(
            *SIGNED_IN,
            Forbidden,
            BookingNotFound,
            BookingNotPayable,
            SimulationNotAllowed,
            ValidationFailed,
        ),
        200: {"model": PaymentRead, "description": "A repeat: the payment that already succeeded."},
        202: {"model": PaymentRead, "description": "The payment is pending: a webhook settles it."},
    },
)
def create_payment(
    data: PaymentCreate,
    user: CurrentUser,
    service: PaymentServiceDep,
    request: Request,
    response: Response,
) -> PaymentRead:
    """Pay a booking. The amount is the booking's, and a repeat returns the live payment."""
    outcome = service.pay(user_id=user.id, booking_id=data.booking_id, simulate=data.simulate)
    response.status_code = pay_status(outcome)
    if outcome.created:
        located(request, response, "get_payment", payment_id=outcome.view.payment.id)
    return payment_read(outcome.view)


@router.get(
    "/{payment_id}",
    summary="One of my payments",
    responses=problems(*SIGNED_IN, Forbidden, PaymentNotFound, ValidationFailed),
)
def get_payment(
    payment_id: uuid.UUID, user: CurrentUser, service: PaymentServiceDep
) -> PaymentRead:
    """A payment of another user answers 403, and an unknown ID answers 404 (ADR-07)."""
    return payment_read(service.get_owned(user.id, payment_id))


# The handler declares no body, so FastAPI cannot document one. This does, and it documents the
# signature header that the dependency reads (architecture §6.5).
WEBHOOK_DOCS = {
    "parameters": [
        {
            "name": "X-Webhook-Signature",
            "in": "header",
            "required": True,
            "description": "`t=<unix seconds>,v1=<hex>`. `v1` is `HMAC_SHA256(secret, '<t>.' + raw"
            " body)`. Outside production, `WEBHOOK_SIGNATURE_REQUIRED=false` skips the check.",
            "schema": {"type": "string"},
            "example": "t=1790000000,v1=afce76fa350828580fc1b9c5ad8784b180b096922bb87e29b10d10"
            "8a7b51b385",
        }
    ],
    "requestBody": {
        "required": True,
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/WebhookPayload"},  # see `create_app`
                "example": {
                    "event_id": "evt_01J8ZQ4M3K7W",
                    "payment_id": "0d4b7a52-3f1e-4d0a-9b8c-2f6a1c7e9d10",
                    "status": "SUCCESS",
                    "provider_payment_id": "sim_0d4b7a523f1e4d0a9b8c2f6a1c7e9d10",
                    "amount": "499.00",
                    "currency": "INR",
                },
            }
        },
    },
}


@router.post(
    "/webhook/",
    summary="Receive a payment event from the provider",
    openapi_extra=WEBHOOK_DOCS,
    responses=problems(WebhookSignatureInvalid, PayloadTooLarge, ValidationFailed),
)
def receive_webhook(
    payload: Annotated[WebhookPayload, Depends(verified_webhook_payload)],
    service: PaymentServiceDep,
) -> WebhookAck:
    """Every signed event with a valid body is stored once. A repeat answers `duplicate`.

    A business failure is not an error: the answer is `200` and `outcome` says `REJECTED` or
    `NEEDS_REVIEW`. `503` asks the provider to retry.
    """
    return service.handle_webhook(payload)

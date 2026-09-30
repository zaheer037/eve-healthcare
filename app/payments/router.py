"""Payment endpoints (spec section 7.2). Each needs a signed-in user, except the webhook."""

import uuid
from http import HTTPStatus
from typing import Annotated

from fastapi import Depends, Request, Response

from app.auth.dependencies import CurrentUser
from app.core.db import SessionDep
from app.core.http import located
from app.core.routing import TolerantRouter
from app.models.payment import PaymentStatus
from app.payments.schemas import BookingRef, PaymentCreate, PaymentRead
from app.payments.service import PaymentService, PaymentView, PayOutcome
from app.payments.simulator import PaymentSimulator

router = TolerantRouter(prefix="/payments", tags=["payments"])


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


@router.get("/{payment_id}", summary="One of my payments")
def get_payment(
    payment_id: uuid.UUID, user: CurrentUser, service: PaymentServiceDep
) -> PaymentRead:
    """A payment of another user answers 403, and an unknown ID answers 404 (decision D3)."""
    return payment_read(service.get_owned(user.id, payment_id))

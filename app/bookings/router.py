"""Booking endpoints (spec section 7.2). Every one needs a signed-in user."""

import uuid
from typing import Annotated

from fastapi import Depends, Request, Response

from app.auth.dependencies import CurrentUser
from app.bookings.schemas import BookingCreate, BookingDetailRead, BookingRead
from app.bookings.service import BookingService
from app.core.db import SessionDep
from app.core.errors import (
    AppointmentOutOfWindow,
    BookingLimitReached,
    BookingNotFound,
    CentreTestNotFound,
    DuplicateBooking,
    Forbidden,
    InvalidBookingStatus,
    InvalidToken,
    NotAuthenticated,
    PaymentInProgress,
    ValidationFailed,
)
from app.core.http import located
from app.core.pagination import Page, PageParamsDep
from app.core.problem import problems
from app.core.routing import TolerantRouter

router = TolerantRouter(prefix="/bookings", tags=["bookings"])


SIGNED_IN = (NotAuthenticated, InvalidToken)  # every booking endpoint needs a token


def get_booking_service(request: Request, session: SessionDep) -> BookingService:
    return BookingService(session, request.app.state.settings)


BookingServiceDep = Annotated[BookingService, Depends(get_booking_service)]


@router.post(
    "/",
    status_code=201,
    summary="Book a test at a centre",
    responses=problems(
        *SIGNED_IN,
        CentreTestNotFound,
        AppointmentOutOfWindow,
        ValidationFailed,
        BookingLimitReached,
        DuplicateBooking,
    ),
)
def create_booking(
    data: BookingCreate,
    user: CurrentUser,
    service: BookingServiceDep,
    request: Request,
    response: Response,
) -> BookingRead:
    """The price is the centre's price now. It is copied, so a later change does not touch it."""
    booking = service.create(
        user_id=user.id,
        centre_id=data.centre_id,
        test_id=data.test_id,
        appointment_at=data.appointment_at,
    )
    located(request, response, "get_booking", booking_id=booking.id)
    return BookingRead.model_validate(booking)


@router.get(
    "/",
    summary="List my bookings, newest first",
    responses=problems(*SIGNED_IN, ValidationFailed),
)
def list_bookings(
    user: CurrentUser, service: BookingServiceDep, page: PageParamsDep
) -> Page[BookingRead]:
    bookings, total = service.list_for_user(user.id, page)
    return Page[BookingRead](
        data=[BookingRead.model_validate(booking) for booking in bookings],
        pagination=page.describe(total),
    )


@router.get(
    "/{booking_id}",
    summary="One of my bookings, with its payments",
    responses=problems(*SIGNED_IN, Forbidden, BookingNotFound, ValidationFailed),
)
def get_booking(
    booking_id: uuid.UUID, user: CurrentUser, service: BookingServiceDep
) -> BookingDetailRead:
    """A booking of another user answers 403, and an unknown ID answers 404 (decision D3)."""
    return BookingDetailRead.model_validate(service.get_owned(user.id, booking_id))


@router.post(
    "/{booking_id}/cancel",
    summary="Cancel one of my bookings",
    responses=problems(
        *SIGNED_IN,
        Forbidden,
        BookingNotFound,
        InvalidBookingStatus,
        PaymentInProgress,
        ValidationFailed,
    ),
)
def cancel_booking(
    booking_id: uuid.UUID, user: CurrentUser, service: BookingServiceDep
) -> BookingRead:
    """Cancelling a cancelled booking answers 200 and changes nothing. A failed booking answers
    409, and so does a booking with a payment in progress."""
    return BookingRead.model_validate(service.cancel(user_id=user.id, booking_id=booking_id))

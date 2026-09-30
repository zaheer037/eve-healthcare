"""Booking endpoints (spec section 7.2). Every one needs a signed-in user."""

from typing import Annotated

from fastapi import Depends, Request, Response

from app.auth.dependencies import CurrentUser
from app.bookings.schemas import BookingCreate, BookingRead
from app.bookings.service import BookingService
from app.core.db import SessionDep
from app.core.routing import TolerantRouter

router = TolerantRouter(prefix="/bookings", tags=["bookings"])


def get_booking_service(request: Request, session: SessionDep) -> BookingService:
    return BookingService(session, request.app.state.settings)


BookingServiceDep = Annotated[BookingService, Depends(get_booking_service)]


@router.post("/", status_code=201, summary="Book a test at a centre")
def create_booking(
    data: BookingCreate, user: CurrentUser, service: BookingServiceDep, response: Response
) -> BookingRead:
    """The price is the centre's price now. It is copied, so a later change does not touch it."""
    booking = service.create(
        user_id=user.id,
        centre_id=data.centre_id,
        test_id=data.test_id,
        appointment_at=data.appointment_at,
    )
    return BookingRead.model_validate(booking)

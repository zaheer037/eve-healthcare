"""Plain functions that insert rows through SQLAlchemy (spec section 11.2).

Each factory fills every required column with a valid value and lets a test override only what
it asserts on. It commits, so the rows are visible to the app's own sessions.
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.models.booking import Booking, BookingStatus
from app.models.catalog import Centre, CentreTest, DiagnosticTest
from app.models.payment import Payment, PaymentStatus, WebhookEvent, WebhookOutcome
from app.models.user import Role, User

NOT_A_HASH = "not-a-real-hash"  # a login test hashes a real password instead


def make_user(
    session: Session,
    *,
    email: str | None = None,
    password_hash: str = NOT_A_HASH,
    role: Role = Role.PATIENT,
    full_name: str | None = None,
    phone: str | None = None,
) -> User:
    user = User(
        email=email or f"user-{uuid.uuid4().hex[:12]}@example.in",
        password_hash=password_hash,
        role=role,
        full_name=full_name,
        phone=phone,
    )
    session.add(user)
    session.commit()
    return user


def make_centre(
    session: Session,
    *,
    name: str | None = None,
    location: str = "12 MG Road",
    city: str | None = "Pune",
    is_active: bool = True,
) -> Centre:
    centre = Centre(
        name=name or f"Centre {uuid.uuid4().hex[:8]}",
        location=location,
        city=city,
        is_active=is_active,
    )
    session.add(centre)
    session.commit()
    return centre


def make_diagnostic_test(
    session: Session,
    *,
    code: str | None = None,
    name: str | None = None,
    description: str | None = None,
    is_active: bool = True,
) -> DiagnosticTest:
    suffix = uuid.uuid4().hex[:8]
    test = DiagnosticTest(
        code=code or f"T{suffix.upper()}",
        name=name or f"Test {suffix}",
        description=description,
        is_active=is_active,
    )
    session.add(test)
    session.commit()
    return test


def make_centre_test(
    session: Session,
    *,
    centre: Centre | None = None,
    test: DiagnosticTest | None = None,
    price: Decimal = Decimal("499.00"),
    currency: str = "INR",
    is_available: bool = True,
) -> CentreTest:
    """The price of a test at a centre. It creates the centre and the test when none is given."""
    centre = centre or make_centre(session)
    test = test or make_diagnostic_test(session)
    offering = CentreTest(
        centre_id=centre.id,
        test_id=test.id,
        price=price,
        currency=currency,
        is_available=is_available,
    )
    session.add(offering)
    session.commit()
    return offering


def future_slot(days: int = 7) -> datetime:
    """A start time some days ahead, on the minute, as the booking schema would store it."""
    return (datetime.now(UTC) + timedelta(days=days)).replace(second=0, microsecond=0)


def make_booking(
    session: Session,
    *,
    user: User | None = None,
    offering: CentreTest | None = None,
    appointment_at: datetime | None = None,
    status: BookingStatus = BookingStatus.PENDING,
    amount: Decimal | None = None,
) -> Booking:
    """A booking of the offering's test at its centre. It creates the user and the offering when
    none is given, and copies the offering's price unless the test sets an amount."""
    user = user or make_user(session)
    offering = offering or make_centre_test(session)
    booking = Booking(
        user_id=user.id,
        centre_id=offering.centre_id,
        test_id=offering.test_id,
        appointment_at=appointment_at or future_slot(),
        amount=offering.price if amount is None else amount,
        currency=offering.currency,
        status=status,
    )
    session.add(booking)
    session.commit()
    return booking


def make_payment(
    session: Session,
    *,
    booking: Booking | None = None,
    status: PaymentStatus = PaymentStatus.PENDING,
    amount: Decimal | None = None,
    provider: str = "SIMULATOR",
    provider_payment_id: str | None = None,
    failure_reason: str | None = None,
) -> Payment:
    """A payment for the booking. A settled payment gets a settlement time, as the CHECK needs."""
    booking = booking or make_booking(session)
    payment = Payment(
        booking_id=booking.id,
        amount=booking.amount if amount is None else amount,
        currency=booking.currency,
        status=status,
        provider=provider,
        provider_payment_id=provider_payment_id,
        failure_reason=failure_reason,
        settled_at=None if status is PaymentStatus.PENDING else datetime.now(UTC),
    )
    session.add(payment)
    session.commit()
    return payment


def make_webhook_event(
    session: Session,
    *,
    provider: str = "SIMULATOR",
    event_id: str | None = None,
    payment: Payment | None = None,
    payload: dict[str, Any] | None = None,
    outcome: WebhookOutcome | None = None,
) -> WebhookEvent:
    """A stored event. An event with an outcome also has a processing time, as the CHECK needs."""
    event = WebhookEvent(
        provider=provider,
        event_id=event_id or f"evt_{uuid.uuid4().hex[:12]}",
        payment_id=payment.id if payment else None,
        payload=payload or {"status": "SUCCESS"},
        outcome=outcome,
        processed_at=datetime.now(UTC) if outcome else None,
    )
    session.add(event)
    session.commit()
    return event

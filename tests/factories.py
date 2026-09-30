"""Plain functions that insert rows through SQLAlchemy (spec section 11.2).

Each factory fills every required column with a valid value and lets a test override only what
it asserts on. It commits, so the rows are visible to the app's own sessions.
"""

import uuid
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models.catalog import Centre, CentreTest, DiagnosticTest
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

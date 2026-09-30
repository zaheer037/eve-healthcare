"""Plain functions that insert rows through SQLAlchemy (spec section 11.2).

Each factory fills every required column with a valid value and lets a test override only what
it asserts on. It commits, so the rows are visible to the app's own sessions.
"""

import uuid

from sqlalchemy.orm import Session

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

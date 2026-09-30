"""Each rule of spec section 5.5 rejects a bad row and accepts a good one."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.db import violated_constraint
from app.models.user import Role, User
from tests.factories import make_user

# ---------------------------------------------------------------- users


def test_a_valid_user_gets_the_defaults(db_session: Session) -> None:
    user = make_user(db_session)
    assert user.role is Role.PATIENT
    assert user.created_at is not None
    assert user.updated_at is not None
    assert user.full_name is None
    assert user.phone is None


def test_an_email_must_be_lower_case(db_session: Session) -> None:
    db_session.add(User(email="Jane@Example.in", password_hash="x"))
    with pytest.raises(IntegrityError) as error:
        db_session.flush()
    assert violated_constraint(error.value) == "ck_users_email_lowercase"


def test_an_email_is_unique(db_session: Session) -> None:
    make_user(db_session, email="jane@example.in")
    db_session.add(User(email="jane@example.in", password_hash="x"))
    with pytest.raises(IntegrityError) as error:
        db_session.flush()
    assert violated_constraint(error.value) == "uq_users_email"


def test_a_role_must_be_patient_or_admin(db_session: Session) -> None:
    """The ORM refuses an unknown member before the database does, so this uses raw SQL."""
    with pytest.raises(IntegrityError) as error:
        db_session.execute(
            text(
                "INSERT INTO users (id, email, password_hash, role)"
                " VALUES (gen_random_uuid(), 'a@example.in', 'x', 'DOCTOR')"
            )
        )
    assert violated_constraint(error.value) == "ck_users_role"


def test_the_database_defaults_a_missing_role_to_patient(db_session: Session) -> None:
    role = db_session.scalar(
        text(
            "INSERT INTO users (id, email, password_hash)"
            " VALUES (gen_random_uuid(), 'b@example.in', 'x') RETURNING role"
        )
    )
    assert role == "PATIENT"


def test_updated_at_moves_when_a_row_changes(db_session: Session) -> None:
    user = make_user(db_session)
    db_session.execute(  # raw SQL has no onupdate, so this puts updated_at safely in the past
        text("UPDATE users SET updated_at = '2020-01-01T00:00:00Z' WHERE id = :id"), {"id": user.id}
    )
    db_session.commit()
    db_session.refresh(user)
    assert user.updated_at.year == 2020
    user.full_name = "Jane Doe"
    db_session.commit()
    assert user.updated_at.year > 2020

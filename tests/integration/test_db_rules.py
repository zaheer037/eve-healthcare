"""Each rule of architecture §3.5 rejects a bad row and accepts a good one."""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.orm import Session

from app.core.db import violated_constraint
from app.models.catalog import Centre, CentreTest, DiagnosticTest
from app.models.user import Role, User
from tests.factories import make_centre, make_centre_test, make_diagnostic_test, make_user

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


# ---------------------------------------------------------------- centres


def test_a_centre_without_a_city_is_stored(db_session: Session) -> None:
    assert make_centre(db_session, city=None).city is None


def test_a_centre_is_active_by_default(db_session: Session) -> None:
    centre = Centre(name="Default Lab", location="1 Main Road")
    db_session.add(centre)
    db_session.commit()
    assert centre.is_active is True


def test_a_name_and_location_pair_is_unique(db_session: Session) -> None:
    make_centre(db_session, name="City Lab", location="12 MG Road")
    make_centre(db_session, name="City Lab", location="7 Park Street")  # another location is fine
    db_session.add(Centre(name="City Lab", location="12 MG Road"))
    with pytest.raises(IntegrityError) as error:
        db_session.flush()
    assert violated_constraint(error.value) == "uq_centres_name_location"


# ---------------------------------------------------------------- diagnostic tests


@pytest.mark.parametrize("code", ["CBC", "HBA1C_2", "AB", "A" * 20])
def test_a_valid_test_code_is_accepted(db_session: Session, code: str) -> None:
    assert make_diagnostic_test(db_session, code=code).code == code


@pytest.mark.parametrize("code", ["cbc", "A", "A" * 21, "CB C", "CBC-1", "CBC\n"])
def test_a_test_code_must_match_the_pattern(db_session: Session, code: str) -> None:
    db_session.add(DiagnosticTest(code=code, name=f"Test {uuid.uuid4().hex}"))
    with pytest.raises(IntegrityError) as error:
        db_session.flush()
    assert violated_constraint(error.value) == "ck_diagnostic_tests_code"


def test_a_test_code_is_unique(db_session: Session) -> None:
    make_diagnostic_test(db_session, code="CBC")
    db_session.add(DiagnosticTest(code="CBC", name="Another name"))
    with pytest.raises(IntegrityError) as error:
        db_session.flush()
    assert violated_constraint(error.value) == "uq_diagnostic_tests_code"


def test_a_test_name_is_unique(db_session: Session) -> None:
    make_diagnostic_test(db_session, name="Complete Blood Count")
    db_session.add(DiagnosticTest(code="OTHER", name="Complete Blood Count"))
    with pytest.raises(IntegrityError) as error:
        db_session.flush()
    assert violated_constraint(error.value) == "uq_diagnostic_tests_name"


# ---------------------------------------------------------------- centre tests (prices)


def test_a_price_is_stored_with_two_places_and_the_default_currency(db_session: Session) -> None:
    offering = make_centre_test(db_session, price=Decimal("499"))
    db_session.refresh(offering)
    assert offering.price == Decimal("499.00")
    assert offering.currency == "INR"
    assert offering.is_available is True


@pytest.mark.parametrize("price", ["0.01", "99999999.99"])
def test_the_price_bounds_are_accepted(db_session: Session, price: str) -> None:
    assert make_centre_test(db_session, price=Decimal(price))


@pytest.mark.parametrize("price", ["0", "0.00", "0.004", "-1", "NaN"])
def test_a_price_outside_the_bounds_is_refused_and_nan_too(db_session: Session, price: str) -> None:
    """A plain CHECK (price > 0) accepts NaN, because PostgreSQL sorts NaN above every number."""
    centre, test = make_centre(db_session), make_diagnostic_test(db_session)
    db_session.add(CentreTest(centre_id=centre.id, test_id=test.id, price=Decimal(price)))
    with pytest.raises(IntegrityError) as error:
        db_session.flush()
    assert violated_constraint(error.value) == "ck_centre_tests_price"


def test_a_price_over_the_column_range_overflows(db_session: Session) -> None:
    """numeric(10,2) holds eight digits before the point. The API keeps clients below this."""
    centre, test = make_centre(db_session), make_diagnostic_test(db_session)
    db_session.add(CentreTest(centre_id=centre.id, test_id=test.id, price=Decimal("100000000.00")))
    with pytest.raises(DataError):
        db_session.flush()


@pytest.mark.parametrize("currency", ["inr", "INRR", "IN", "R\u20b9P", ""])
def test_a_currency_must_be_three_upper_case_letters(db_session: Session, currency: str) -> None:
    centre, test = make_centre(db_session), make_diagnostic_test(db_session)
    db_session.add(
        CentreTest(centre_id=centre.id, test_id=test.id, price=Decimal("10"), currency=currency)
    )
    with pytest.raises(IntegrityError) as error:
        db_session.flush()
    assert violated_constraint(error.value) == "ck_centre_tests_currency"


def test_another_three_letter_currency_is_accepted(db_session: Session) -> None:
    assert make_centre_test(db_session, currency="USD").currency == "USD"


def test_a_centre_and_a_test_can_be_paired_once(db_session: Session) -> None:
    """Raw SQL: the ORM's identity map would warn about a second object with the same key first."""
    offering = make_centre_test(db_session)
    with pytest.raises(IntegrityError) as error:
        db_session.execute(
            text("INSERT INTO centre_tests (centre_id, test_id, price) VALUES (:c, :t, 5)"),
            {"c": offering.centre_id, "t": offering.test_id},
        )
    assert violated_constraint(error.value) == "pk_centre_tests"


def test_a_price_for_an_unknown_centre_is_refused(db_session: Session) -> None:
    test = make_diagnostic_test(db_session)
    db_session.add(CentreTest(centre_id=uuid.uuid4(), test_id=test.id, price=Decimal("10")))
    with pytest.raises(IntegrityError) as error:
        db_session.flush()
    assert violated_constraint(error.value) == "fk_centre_tests_centre_id"


def test_a_price_for_an_unknown_test_is_refused(db_session: Session) -> None:
    centre = make_centre(db_session)
    db_session.add(CentreTest(centre_id=centre.id, test_id=uuid.uuid4(), price=Decimal("10")))
    with pytest.raises(IntegrityError) as error:
        db_session.flush()
    assert violated_constraint(error.value) == "fk_centre_tests_test_id"


def test_history_cannot_disappear_a_centre_with_prices_cannot_be_deleted(
    db_session: Session,
) -> None:
    offering = make_centre_test(db_session)
    with pytest.raises(IntegrityError) as error:
        db_session.execute(text("DELETE FROM centres WHERE id = :id"), {"id": offering.centre_id})
    assert violated_constraint(error.value) == "fk_centre_tests_centre_id"


def test_a_test_with_prices_cannot_be_deleted(db_session: Session) -> None:
    offering = make_centre_test(db_session)
    with pytest.raises(IntegrityError) as error:
        db_session.execute(
            text("DELETE FROM diagnostic_tests WHERE id = :id"), {"id": offering.test_id}
        )
    assert violated_constraint(error.value) == "fk_centre_tests_test_id"

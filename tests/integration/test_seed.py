"""The seed script on a real database: idempotent, careful with existing data, and the admin can
log in on both endpoints (T2.5)."""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.core.config import Environment, Settings
from app.core.security import hash_password, verify_password
from app.models.catalog import Centre, CentreTest, DiagnosticTest
from app.models.user import Role, User
from scripts.seed import SeedError, SeedReport, run, seed
from scripts.seed_data import CENTRES, TESTS
from tests.factories import make_centre, make_diagnostic_test, make_user

ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "dev-only-admin-password"
FIRST_RUN = SeedReport(admin_created=True, tests=12, centres=5, prices=30)
NOTHING_NEW = SeedReport(admin_created=False, tests=0, centres=0, prices=0)


@pytest.fixture
def seed_settings(test_settings: Settings) -> Settings:
    return test_settings.model_copy(
        update={"admin_email": ADMIN_EMAIL, "admin_password": SecretStr(ADMIN_PASSWORD)}
    )


def run_seed(db_engine: Engine, settings: Settings) -> SeedReport:
    with Session(db_engine, expire_on_commit=False) as session:
        return seed(session, settings)


def count(session: Session, model: type[User] | type[Centre] | type[DiagnosticTest]) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


def count_prices(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(CentreTest)) or 0


def snapshot(session: Session) -> list[tuple[object, ...]]:
    """Every seeded row with its timestamps, so a second run must leave them all as they were."""
    session.expire_all()
    rows: list[tuple[object, ...]] = []
    for model in (User, Centre, DiagnosticTest, CentreTest):
        rows += [tuple(row) for row in session.execute(select(model.__table__))]
    return sorted(rows, key=repr)


# ==== a first run


def test_the_first_run_creates_the_admin_and_the_sample_catalog(
    db_engine: Engine, db_session: Session, seed_settings: Settings
) -> None:
    assert run_seed(db_engine, seed_settings) == FIRST_RUN
    assert (count(db_session, Centre), count(db_session, DiagnosticTest)) == (5, 12)
    assert count_prices(db_session) == 30
    admin = db_session.scalars(select(User)).one()
    assert (admin.email, admin.role) == (ADMIN_EMAIL, Role.ADMIN)
    assert verify_password(ADMIN_PASSWORD, admin.password_hash)


def test_the_seeded_catalog_matches_the_sample_data(
    db_engine: Engine, db_session: Session, seed_settings: Settings
) -> None:
    run_seed(db_engine, seed_settings)
    prices = {
        (centre.name, test.code): price
        for centre, test, price in db_session.execute(
            select(Centre, DiagnosticTest, CentreTest.price)
            .join(CentreTest, CentreTest.centre_id == Centre.id)
            .join(DiagnosticTest, DiagnosticTest.id == CentreTest.test_id)
        )
    }
    expected = {
        (centre.name, code): Decimal(rupees)
        for centre in CENTRES
        for code, rupees in centre.prices.items()
    }
    assert prices == expected
    codes = set(db_session.scalars(select(DiagnosticTest.code)))
    assert codes == {test.code for test in TESTS}


def test_the_seeded_catalog_is_visible_through_the_public_api(
    client: TestClient, db_engine: Engine, seed_settings: Settings, db_session: Session
) -> None:
    run_seed(db_engine, seed_settings)
    assert client.get("/centres/").json()["pagination"]["total"] == 5
    assert client.get("/tests/").json()["pagination"]["total"] == 12
    cbc = db_session.scalars(select(DiagnosticTest).where(DiagnosticTest.code == "CBC")).one()
    offering = client.get("/centres/", params={"test_id": str(cbc.id)}).json()["data"]
    assert offering  # at least one centre offers a blood count
    assert all(item["price"] is not None for item in offering)


def test_the_seeded_admin_logs_in_on_both_endpoints_and_can_write_the_catalog(
    client: TestClient, db_engine: Engine, seed_settings: Settings
) -> None:
    run_seed(db_engine, seed_settings)
    json_login = client.post("/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    form_login = client.post(
        "/auth/token", data={"username": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
    )
    assert (json_login.status_code, form_login.status_code) == (200, 200)
    headers = {"Authorization": f"Bearer {form_login.json()['access_token']}"}
    created = client.post(
        "/centres/", json={"name": "Written By Admin", "location": "1 Road"}, headers=headers
    )
    assert created.status_code == 201


# ==== running it again


def test_a_second_run_changes_nothing(
    db_engine: Engine, db_session: Session, seed_settings: Settings
) -> None:
    run_seed(db_engine, seed_settings)
    before = snapshot(db_session)
    assert run_seed(db_engine, seed_settings) == NOTHING_NEW
    assert snapshot(db_session) == before  # the same rows, the same hash, the same timestamps


def test_an_edit_made_after_the_first_run_survives_the_next(
    client: TestClient, db_engine: Engine, db_session: Session, seed_settings: Settings
) -> None:
    run_seed(db_engine, seed_settings)
    centre = db_session.scalars(select(Centre).order_by(Centre.name)).first()
    assert centre is not None
    price = db_session.scalars(select(CentreTest).where(CentreTest.centre_id == centre.id)).first()
    assert price is not None
    price.price = Decimal("1.00")
    centre.is_active = False
    db_session.commit()
    assert run_seed(db_engine, seed_settings) == NOTHING_NEW
    db_session.expire_all()
    assert price.price == Decimal("1.00")  # not reset to the sample price
    assert centre.is_active is False  # not restored
    assert count(db_session, Centre) == 5  # and not added a second time


def test_an_existing_admin_keeps_its_password_and_the_catalog_is_still_seeded(
    db_engine: Engine, db_session: Session, seed_settings: Settings
) -> None:
    other_hash = hash_password("a-password-the-admin-chose")
    make_user(db_session, email=ADMIN_EMAIL, password_hash=other_hash, role=Role.ADMIN)
    assert run_seed(db_engine, seed_settings) == SeedReport(
        admin_created=False, tests=12, centres=5, prices=30
    )
    admin = db_session.scalars(select(User)).one()
    assert admin.password_hash == other_hash


# ==== refusals


def test_a_patient_with_the_admin_email_is_not_promoted_and_nothing_is_written(
    db_engine: Engine, db_session: Session, seed_settings: Settings
) -> None:
    """A stranger who signed up with the admin's address must not become the admin."""
    make_user(db_session, email=ADMIN_EMAIL, role=Role.PATIENT)
    with pytest.raises(SeedError, match="not an admin"):
        run_seed(db_engine, seed_settings)
    db_session.expire_all()
    assert db_session.scalars(select(User)).one().role is Role.PATIENT
    assert (count(db_session, Centre), count(db_session, DiagnosticTest)) == (0, 0)


def test_production_refuses_the_development_password_before_it_touches_the_database(
    db_engine: Engine, db_session: Session, seed_settings: Settings
) -> None:
    production = seed_settings.model_copy(update={"environment": Environment.PRODUCTION})
    with pytest.raises(SeedError, match="placeholder"):
        run_seed(db_engine, production)
    assert count(db_session, User) == 0
    assert count(db_session, Centre) == 0


# ==== data that an admin created by hand first


def test_a_test_that_already_has_a_sample_code_is_reused_and_priced(
    db_engine: Engine, db_session: Session, seed_settings: Settings
) -> None:
    mine = make_diagnostic_test(db_session, code="CBC", name="Full blood picture")
    report = run_seed(db_engine, seed_settings)
    assert report.tests == 11  # CBC was there already
    assert count(db_session, DiagnosticTest) == 12
    priced = db_session.scalar(
        select(func.count()).select_from(CentreTest).where(CentreTest.test_id == mine.id)
    )
    assert priced == sum("CBC" in centre.prices for centre in CENTRES)
    assert db_session.get(DiagnosticTest, mine.id).name == "Full blood picture"  # type: ignore[union-attr]


def test_a_centre_that_already_exists_is_reused_and_priced(
    db_engine: Engine, db_session: Session, seed_settings: Settings
) -> None:
    sample = CENTRES[0]
    mine = make_centre(db_session, name=sample.name, location=sample.location, city="Elsewhere")
    report = run_seed(db_engine, seed_settings)
    assert report.centres == 4
    priced = db_session.scalar(
        select(func.count()).select_from(CentreTest).where(CentreTest.centre_id == mine.id)
    )
    assert priced == len(sample.prices)


# ==== the command's exit code


def test_run_returns_zero_and_logs_the_counts_without_the_password(
    seed_settings: Settings, db_session: Session, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(seed_settings) == 0
    output = capsys.readouterr().out
    assert '"event": "seed.done"' in output
    assert '"prices": 30' in output
    assert ADMIN_PASSWORD not in output
    assert count(db_session, Centre) == 5


def test_run_returns_one_and_says_why_when_the_seed_refuses(
    seed_settings: Settings, db_session: Session, capsys: pytest.CaptureFixture[str]
) -> None:
    make_user(db_session, email=ADMIN_EMAIL, role=Role.PATIENT)
    assert run(seed_settings) == 1
    output = capsys.readouterr().out
    assert '"event": "seed.refused"' in output
    assert "not an admin" in output
    assert ADMIN_EMAIL not in output  # no personal data in the log

"""The seed script without a database: the sample data and the admin's credentials (T2.5)."""

from dataclasses import asdict

import pytest

from app.catalog.schemas import CentreCreate, CentreTestWrite, DiagnosticTestCreate
from scripts.seed import SeedError, admin_credentials
from scripts.seed_data import CENTRES, TESTS
from tests.support import SettingsFactory

GOOD_PRODUCTION_PASSWORD = "correct horse battery staple"


# ==== the sample data obeys the same rules as the API


def test_the_sample_data_has_the_size_that_the_spec_promises() -> None:
    assert len(CENTRES) == 5
    assert len(TESTS) == 12
    assert sum(len(centre.prices) for centre in CENTRES) == 30


def test_every_sample_test_passes_the_create_schema() -> None:
    for test in TESTS:
        DiagnosticTestCreate.model_validate(asdict(test))


def test_every_sample_centre_passes_the_create_schema() -> None:
    for centre in CENTRES:
        CentreCreate.model_validate(
            {"name": centre.name, "location": centre.location, "city": centre.city}
        )


def test_every_sample_price_passes_the_price_schema() -> None:
    for centre in CENTRES:
        for rupees in centre.prices.values():
            CentreTestWrite.model_validate({"price": str(rupees)})


def test_the_natural_keys_are_unique() -> None:
    assert len({test.code for test in TESTS}) == len(TESTS)
    assert len({test.name for test in TESTS}) == len(TESTS)
    assert len({(centre.name, centre.location) for centre in CENTRES}) == len(CENTRES)


def test_every_price_names_a_known_test_and_every_test_is_offered_somewhere() -> None:
    codes = {test.code for test in TESTS}
    offered = {code for centre in CENTRES for code in centre.prices}
    assert offered == codes


def test_every_centre_offers_at_least_one_test() -> None:
    assert all(centre.prices for centre in CENTRES)


# ==== the admin's credentials


def test_the_credentials_are_normalised_like_a_login(make_settings: SettingsFactory) -> None:
    settings = make_settings(admin_email="  Admin@Example.COM ", admin_password="dev-only-pw-1")
    assert admin_credentials(settings) == ("admin@example.com", "dev-only-pw-1")


@pytest.mark.parametrize("environment", ["development", "test"])
def test_the_development_password_is_accepted_outside_production(
    make_settings: SettingsFactory, environment: str
) -> None:
    settings = make_settings(
        environment=environment,
        admin_email="admin@example.com",
        admin_password="dev-only-admin-password",
    )
    assert admin_credentials(settings)[1] == "dev-only-admin-password"


@pytest.mark.parametrize(
    "settings_kwargs",
    [
        {},
        {"admin_email": "admin@example.com"},
        {"admin_password": "dev-only-admin-password"},
        {"admin_email": "", "admin_password": "dev-only-admin-password"},
    ],
)
def test_a_missing_email_or_password_is_refused(
    make_settings: SettingsFactory, settings_kwargs: dict[str, str]
) -> None:
    with pytest.raises(SeedError, match="ADMIN_EMAIL and ADMIN_PASSWORD"):
        admin_credentials(make_settings(**settings_kwargs))


@pytest.mark.parametrize(
    ("email", "password"),
    [
        ("   ", "dev-only-admin-password"),  # a blank address
        ("a" * 255, "dev-only-admin-password"),  # longer than a login accepts
        ("admin@example.com", "x" * 129),  # longer than a login accepts
        ("admin@example.com", "bad\x00password"),  # a control character
    ],
)
def test_credentials_that_the_login_form_would_refuse_are_refused_and_never_echoed(
    make_settings: SettingsFactory, email: str, password: str
) -> None:
    settings = make_settings(admin_email=email, admin_password=password)
    with pytest.raises(SeedError, match="login") as refused:
        admin_credentials(settings)
    assert password not in str(refused.value)
    assert refused.value.__cause__ is None  # the validation error would echo the input


@pytest.mark.parametrize(
    "password",
    ["dev-only-admin-password", "change-me-please-change-me", "whsec_demo_only_0123456789"],
)
def test_production_refuses_a_placeholder_password(
    make_settings: SettingsFactory, password: str
) -> None:
    settings = make_settings(
        environment="production", admin_email="admin@example.com", admin_password=password
    )
    with pytest.raises(SeedError, match="placeholder"):
        admin_credentials(settings)


def test_production_refuses_a_password_shorter_than_the_minimum(
    make_settings: SettingsFactory,
) -> None:
    settings = make_settings(
        environment="production",
        admin_email="admin@example.com",
        admin_password="x" * 14,
        password_min_length=15,
    )
    with pytest.raises(SeedError, match="15 characters"):
        admin_credentials(settings)


def test_production_accepts_a_strong_password(make_settings: SettingsFactory) -> None:
    settings = make_settings(
        environment="production",
        admin_email="ops@hospital.example",
        admin_password=GOOD_PRODUCTION_PASSWORD,
    )
    assert admin_credentials(settings) == ("ops@hospital.example", GOOD_PRODUCTION_PASSWORD)

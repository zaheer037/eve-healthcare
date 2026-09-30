"""Settings: defaults per environment, and every start-up refusal (AB26)."""

import pytest
from pydantic import ValidationError

from app.core.config import Environment, Settings
from tests.support import SettingsFactory

STRONG_JWT = "j" * 40
STRONG_WEBHOOK = "w" * 40
DEV_JWT = "dev-only-jwt-secret-change-me-0123456789abcdef"
DEV_WEBHOOK = "dev-only-webhook-secret-change-me-0123456789"
DOCUMENTED_WEBHOOK = "whsec_demo_only_not_a_real_secret_0123456789"
CI_JWT = "ci-only-jwt-secret-0123456789abcdef0123456789"


def test_defaults_in_development(make_settings: SettingsFactory) -> None:
    s = make_settings()
    assert s.environment is Environment.DEVELOPMENT
    assert s.password_min_length == 15
    assert s.access_token_minutes == 30
    assert s.hash_max_concurrency == 4
    assert s.db_lock_timeout_ms == 2000
    assert s.webhook_signature_required is True
    assert s.docs_enabled is True
    assert s.payment_allow_simulate_override is True
    assert s.auth_ip_limit_per_minute == 300
    assert s.cors_origins == []


def test_production_defaults_are_the_safe_ones(make_settings: SettingsFactory) -> None:
    s = make_settings(environment="production")
    assert s.docs_enabled is False
    assert s.payment_allow_simulate_override is False
    assert s.auth_ip_limit_per_minute == 30


def test_an_operator_can_turn_docs_on_in_production(make_settings: SettingsFactory) -> None:
    assert make_settings(environment="production", docs_enabled=True).docs_enabled is True


def test_environment_has_no_default(make_settings: SettingsFactory) -> None:
    """A typo such as ENVIRONMNET must stop the start-up instead of falling back to development."""
    with pytest.raises(ValidationError, match="environment"):
        Settings(
            _env_file=None,
            database_url="postgresql+psycopg://u:p@localhost/db",
            jwt_secret=STRONG_JWT,
            webhook_secret=STRONG_WEBHOOK,
        )


def test_an_unknown_environment_is_refused(make_settings: SettingsFactory) -> None:
    with pytest.raises(ValidationError, match="environment"):
        make_settings(environment="staging")


@pytest.mark.parametrize("name", ["jwt_secret", "webhook_secret"])
def test_a_short_secret_is_refused_in_every_environment(
    make_settings: SettingsFactory, name: str
) -> None:
    with pytest.raises(ValidationError, match="shorter than 32 bytes"):
        make_settings(**{name: "s" * 31})


def test_a_32_byte_secret_is_accepted(make_settings: SettingsFactory) -> None:
    assert make_settings(jwt_secret="s" * 32).jwt_secret.get_secret_value() == "s" * 32


def test_the_length_is_counted_in_bytes(make_settings: SettingsFactory) -> None:
    """Sixteen two-byte characters are 32 bytes. Thirty-one ASCII characters are not."""
    assert make_settings(jwt_secret="é" * 16)
    with pytest.raises(ValidationError, match="shorter than 32 bytes"):
        make_settings(jwt_secret="é" * 15 + "a")


@pytest.mark.parametrize("environment", ["test", "production"])
@pytest.mark.parametrize(
    "placeholder", [DEV_JWT, DEV_WEBHOOK, DOCUMENTED_WEBHOOK, CI_JWT, "change-me" * 5]
)
def test_a_placeholder_secret_is_refused_outside_development(
    make_settings: SettingsFactory, environment: str, placeholder: str
) -> None:
    with pytest.raises(ValidationError, match="placeholder"):
        make_settings(environment=environment, jwt_secret=placeholder)
    with pytest.raises(ValidationError, match="placeholder"):
        make_settings(environment=environment, webhook_secret=placeholder)


def test_development_accepts_the_compose_placeholders(make_settings: SettingsFactory) -> None:
    assert make_settings(jwt_secret=DEV_JWT, webhook_secret=DEV_WEBHOOK)


def test_production_refuses_an_unsigned_webhook(make_settings: SettingsFactory) -> None:
    with pytest.raises(ValidationError, match="webhook signature"):
        make_settings(environment="production", webhook_signature_required=False)


def test_other_environments_may_turn_the_signature_off(make_settings: SettingsFactory) -> None:
    assert make_settings(environment="test", webhook_signature_required=False)


def test_production_refuses_the_simulate_override(make_settings: SettingsFactory) -> None:
    with pytest.raises(ValidationError, match="simulate override"):
        make_settings(environment="production", payment_allow_simulate_override=True)


@pytest.mark.parametrize(
    "value",
    ["*", "10.0.0.5, *", " * ", "0.0.0.0/0", "::/0", "10.0.0.5,0.0.0.0/0", "192.168.1.1/0"],
)
def test_production_refuses_a_proxy_list_that_trusts_every_address(
    make_settings: SettingsFactory, value: str
) -> None:
    """Uvicorn takes the leftmost `X-Forwarded-For` entry when every hop is trusted, and the
    client writes that entry. A network of prefix length 0 trusts every hop as `*` does."""
    with pytest.raises(ValidationError, match="FORWARDED_ALLOW_IPS"):
        make_settings(environment="production", forwarded_allow_ips=value)


@pytest.mark.parametrize(
    "value", ["10.0.0.0/8,172.16.0.5", "127.0.0.1,::1", "fd00::/8", "10.0.0.0/8, proxy.internal"]
)
def test_a_named_proxy_network_is_accepted_in_production(
    make_settings: SettingsFactory, value: str
) -> None:
    assert make_settings(environment="production", forwarded_allow_ips=value)


def test_development_accepts_the_wildcard(make_settings: SettingsFactory) -> None:
    assert make_settings(forwarded_allow_ips="*")


def test_cors_origins_parse_from_a_comma_separated_string(
    make_settings: SettingsFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NoDecode stops pydantic-settings from demanding JSON for a list field."""
    monkeypatch.setenv("CORS_ORIGINS", "https://a.example, https://b.example")
    assert make_settings().cors_origins == ["https://a.example", "https://b.example"]


@pytest.mark.parametrize("length", [7, 129])
def test_the_password_length_setting_has_bounds(
    make_settings: SettingsFactory, length: int
) -> None:
    with pytest.raises(ValidationError):
        make_settings(password_min_length=length)


def test_a_lower_minimum_is_allowed_for_a_friendlier_demo(make_settings: SettingsFactory) -> None:
    assert make_settings(password_min_length=8).password_min_length == 8


def test_secrets_stay_out_of_the_repr(make_settings: SettingsFactory) -> None:
    text = repr(make_settings(jwt_secret="k" * 40, admin_password="hunter2-hunter2-hunter2"))
    assert "k" * 40 not in text
    assert "hunter2" not in text
    assert "u:p@" not in text


def test_the_process_environment_wins_over_defaults(
    make_settings: SettingsFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACCESS_TOKEN_MINUTES", "5")
    assert make_settings().access_token_minutes == 5

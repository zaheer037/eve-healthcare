"""Settings: one class reads the environment and is validated at start-up (spec section 10.3).

The application does not start when a value is invalid. `ENVIRONMENT` has no default, so a
typo in its name stops the start-up instead of falling back to development defaults.
"""

from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

MIN_SECRET_BYTES = 32
# The compose defaults and the example secret in the spec start with one of these.
PLACEHOLDER_PREFIXES = ("dev-only-", "whsec_demo_only_", "change-me")


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


def _is_production(data: dict[str, Any]) -> bool:
    # `environment` is absent from `data` when its own validation failed. That error comes first.
    return data.get("environment") == Environment.PRODUCTION


class Settings(BaseSettings):
    # extra="ignore": compose shares one environment with the database container.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: Environment  # required on purpose: no default

    # Database
    database_url: SecretStr
    db_pool_size: int = Field(default=10, ge=1)
    db_max_overflow: int = Field(default=10, ge=0)
    db_pool_timeout: int = Field(default=10, ge=1)
    db_statement_timeout_ms: int = Field(default=5000, ge=1)
    # Two lock timeouts stay inside the 5-second webhook budget (spec section 8.4).
    db_lock_timeout_ms: int = Field(default=2000, ge=1)

    # Authentication
    jwt_secret: SecretStr
    jwt_issuer: str = "eve-healthcare-auth"
    jwt_audience: str = "eve-healthcare-api"
    access_token_minutes: int = Field(default=30, ge=1)
    password_min_length: int = Field(default=15, ge=8, le=128)  # decision D12
    hash_max_concurrency: int = Field(default=4, ge=1)
    login_max_failures: int = Field(default=10, ge=1)
    login_failure_window_minutes: int = Field(default=15, ge=1)
    # Host-local Docker clients share one bridge address, so development allows more.
    auth_ip_limit_per_minute: int = Field(
        default_factory=lambda data: 30 if _is_production(data) else 300, ge=1
    )

    # Payments and webhook
    webhook_secret: SecretStr
    webhook_signature_required: bool = True  # production refuses False (decision D11)
    webhook_tolerance_seconds: int = Field(default=300, ge=1)
    payment_success_rate: float = Field(default=0.8, ge=0, le=1)
    payment_allow_simulate_override: bool = Field(
        default_factory=lambda data: not _is_production(data)
    )

    # Bookings
    booking_min_lead_minutes: int = Field(default=30, ge=0)
    booking_max_advance_days: int = Field(default=365, ge=1)
    booking_max_active_per_user: int = Field(default=20, ge=1)

    # HTTP
    request_body_limit_bytes: int = Field(default=65536, ge=1)
    docs_enabled: bool = Field(default_factory=lambda data: not _is_production(data))
    # Uvicorn reads the same variable. `Settings` reads it too, so production can refuse "*".
    forwarded_allow_ips: str = "127.0.0.1,::1"
    cors_origins: Annotated[list[str], NoDecode] = []

    # Logging
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"

    # Read by the seed script only
    admin_email: str | None = None
    admin_password: SecretStr | None = None

    @field_validator("cors_origins", mode="before")
    @classmethod
    def split_origins(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @model_validator(mode="after")
    def refuse_unsafe_values(self) -> "Settings":
        for name in ("jwt_secret", "webhook_secret"):
            secret = getattr(self, name).get_secret_value()
            if len(secret.encode()) < MIN_SECRET_BYTES:
                raise ValueError(f"{name} is shorter than {MIN_SECRET_BYTES} bytes")
            if self.environment is not Environment.DEVELOPMENT and secret.startswith(
                PLACEHOLDER_PREFIXES
            ):
                raise ValueError(f"{name} is a placeholder")
        if self.environment is Environment.PRODUCTION:
            if not self.webhook_signature_required:
                raise ValueError("the webhook signature cannot be disabled in production")
            if self.payment_allow_simulate_override:
                raise ValueError("the simulate override cannot be enabled in production")
            if "*" in {entry.strip() for entry in self.forwarded_allow_ips.split(",")}:
                raise ValueError("FORWARDED_ALLOW_IPS cannot trust every address in production")
        return self


@lru_cache
def get_settings() -> Settings:
    """The cached settings. Tests override this dependency."""
    return Settings()  # the required values come from the environment

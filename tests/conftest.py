"""Fixtures shared by the unit and the integration tests."""

from typing import Any

import pytest

from app.core.config import Settings
from tests.support import SettingsFactory

_TEST_DEFAULTS: dict[str, Any] = {
    "environment": "development",
    "database_url": "postgresql+psycopg://eve:eve-test@localhost:5432/eve_test",
    "jwt_secret": "j" * 40,
    "webhook_secret": "w" * 40,
}


@pytest.fixture
def make_settings(monkeypatch: pytest.MonkeyPatch) -> SettingsFactory:
    """Build Settings from keyword arguments only: no `.env` file and no ambient variables."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)

    def factory(**overrides: Any) -> Settings:
        return Settings(_env_file=None, **{**_TEST_DEFAULTS, **overrides})

    return factory


@pytest.fixture
def settings(make_settings: SettingsFactory) -> Settings:
    return make_settings(environment="test")

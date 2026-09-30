"""Fixtures shared by the unit and the integration tests."""

import logging
from collections.abc import Iterator
from typing import Any

import pytest
import structlog

from app.core.config import Settings
from app.core.logging import DropTraceback
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


@pytest.fixture(autouse=True)
def restore_global_logging() -> Iterator[None]:
    """`configure_logging` changes structlog and two Uvicorn loggers. Undo that after each test."""
    yield
    structlog.reset_defaults()
    logging.getLogger("uvicorn.access").disabled = False
    error_logger = logging.getLogger("uvicorn.error")
    for existing in [f for f in error_logger.filters if isinstance(f, DropTraceback)]:
        error_logger.removeFilter(existing)

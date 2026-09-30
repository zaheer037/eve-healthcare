"""Helpers shared by test modules. Fixtures live in conftest.py."""

from collections.abc import Callable

from app.core.config import Settings

SettingsFactory = Callable[..., Settings]

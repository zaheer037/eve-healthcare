"""Integration fixtures: a real PostgreSQL, the migrated schema, the app and its client."""

from collections.abc import Callable, Iterator
from contextlib import ExitStack
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import Engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session
from structlog.contextvars import merge_contextvars
from structlog.testing import capture_logs

from app.core.config import Settings
from app.core.db import create_db_engine
from app.core.security import create_access_token
from app.main import create_app
from app.models.base import Base
from app.models.user import Role, User
from tests.database import ensure_database, migrate, reset_schema
from tests.factories import make_user
from tests.support import QueryCounter

TEST_JWT_SECRET = "test-jwt-secret-0123456789abcdef0123456789"
TEST_WEBHOOK_SECRET = "test-webhook-secret-0123456789abcdef0123"


class IntegrationSettings(BaseSettings):
    """Where the test database lives. The default is the compose database (`make db`)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    test_database_url: str = "postgresql+psycopg://eve:eve-dev-password@localhost:5432/eve_test"


@pytest.fixture(scope="session")
def migrated_database() -> URL:
    """Create the database if it is missing, empty it and run every migration once."""
    url = make_url(IntegrationSettings().test_database_url)
    ensure_database(url)
    reset_schema(url)
    migrate(url)
    return url


@pytest.fixture(scope="session")
def test_settings(migrated_database: URL) -> Settings:
    with pytest.MonkeyPatch.context() as patch:  # no ambient variable may change the settings
        for name in Settings.model_fields:
            patch.delenv(name.upper(), raising=False)
        return Settings(
            _env_file=None,
            environment="test",
            database_url=migrated_database.render_as_string(hide_password=False),
            jwt_secret=TEST_JWT_SECRET,
            webhook_secret=TEST_WEBHOOK_SECRET,
        )


@pytest.fixture(scope="session")
def db_engine(test_settings: Settings) -> Iterator[Engine]:
    engine = create_db_engine(test_settings)
    yield engine
    engine.dispose()  # with filterwarnings=error, an unclosed connection would fail the run


@pytest.fixture(scope="session")
def app(test_settings: Settings) -> FastAPI:
    return create_app(test_settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    # `with` runs the lifespan, so the engine, the session factory and the throttle are new per
    # test, and the catch-all 500 handler can be tested (raise_server_exceptions=False).
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def client_with(test_settings: Settings) -> Iterator[Callable[..., TestClient]]:
    """Build a client for an app whose settings differ from the default test settings.

    `client_with(auth_ip_limit_per_minute=3)` starts a new app with that limit. Every client
    that a test builds is closed when the test ends.
    """
    stack = ExitStack()

    def build(**overrides: Any) -> TestClient:
        settings = test_settings.model_copy(update=overrides)
        application = create_app(settings)
        return stack.enter_context(TestClient(application, raise_server_exceptions=False))

    yield build
    stack.close()


@pytest.fixture
def db_session(db_engine: Engine) -> Iterator[Session]:
    """A session for arranging and asserting. It is separate from the app's sessions."""
    with Session(db_engine, expire_on_commit=False) as session:
        yield session


@pytest.fixture(autouse=True)
def clean_database(db_engine: Engine) -> Iterator[None]:
    """Empty every table after each test. Real commits happen, so races stay testable."""
    yield
    tables = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
    if tables:
        with db_engine.begin() as connection:
            connection.execute(text("SET LOCAL lock_timeout = '5s'"))  # TRUNCATE locks the tables
            connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


@pytest.fixture
def query_counter(app: FastAPI, client: TestClient) -> QueryCounter:
    """Counts the statements of the app's engine. It exists once the client has started the app."""
    return QueryCounter(app.state.engine)


@pytest.fixture
def logs(client: TestClient) -> Iterator[list[dict[str, Any]]]:
    """The structlog events of a test. It starts after the client, because the lifespan
    reconfigures logging."""
    with capture_logs(processors=[merge_contextvars]) as captured:  # `request_id` is bound there
        yield captured


@pytest.fixture
def auth_headers(test_settings: Settings) -> Callable[[User], dict[str, str]]:
    """`auth_headers(user)` is the `Authorization` header of a valid token for that user."""

    def build(user: User) -> dict[str, str]:
        token, _ = create_access_token(user.id, test_settings)
        return {"Authorization": f"Bearer {token}"}

    return build


@pytest.fixture
def patient(db_session: Session) -> User:
    return make_user(db_session)


@pytest.fixture
def admin(db_session: Session) -> User:
    return make_user(db_session, role=Role.ADMIN)


@pytest.fixture
def patient_headers(
    patient: User, auth_headers: Callable[[User], dict[str, str]]
) -> dict[str, str]:
    return auth_headers(patient)


@pytest.fixture
def admin_headers(admin: User, auth_headers: Callable[[User], dict[str, str]]) -> dict[str, str]:
    return auth_headers(admin)

"""The app factory: middleware order, docs switch, CORS, health probes and the lazy entrypoint."""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.main
from app.core.config import Settings, get_settings
from app.main import create_app
from tests.support import SettingsFactory

UNREACHABLE_DATABASE = "postgresql+psycopg://eve:x@127.0.0.1:1/eve"  # nothing listens on port 1


@contextmanager
def running(settings: Settings) -> Iterator[TestClient]:
    # `with` runs the lifespan: the engine and the session factory exist only inside it.
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        yield client


@pytest.fixture
def client(make_settings: SettingsFactory) -> Iterator[TestClient]:
    with running(
        make_settings(environment="test", database_url=UNREACHABLE_DATABASE)
    ) as test_client:
        yield test_client


def test_every_response_has_a_request_id_and_no_store(client: TestClient) -> None:
    response = client.get("/health/live")
    assert response.headers["x-request-id"]
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"


def test_an_unknown_route_is_a_problem_response_with_the_request_id(client: TestClient) -> None:
    response = client.get("/nowhere")
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["request_id"] == response.headers["x-request-id"]


@pytest.mark.parametrize("path", ["/health/live", "/health/live/"])
def test_liveness_answers_in_both_spellings_without_a_database(
    client: TestClient, path: str
) -> None:
    response = client.get(path, follow_redirects=False)
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_liveness_answers_head(client: TestClient) -> None:
    assert client.head("/health/live").status_code == 200


def test_readiness_is_a_503_problem_when_the_database_is_down(client: TestClient) -> None:
    response = client.get("/health/ready")
    body = response.json()
    assert response.status_code == 503
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["retry-after"] == "1"
    assert body["code"] == "SERVICE_UNAVAILABLE"
    assert "127.0.0.1" not in response.text  # no connection detail leaks


def test_the_docs_are_on_outside_production(client: TestClient) -> None:
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200


def test_the_docs_are_off_in_production(make_settings: SettingsFactory) -> None:
    settings = make_settings(environment="production", database_url=UNREACHABLE_DATABASE)
    with running(settings) as production_client:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert production_client.get(path).status_code == 404


def test_cors_is_off_by_default(client: TestClient) -> None:
    response = client.get("/health/live", headers={"Origin": "https://a.example"})
    assert "access-control-allow-origin" not in response.headers


def test_cors_answers_a_preflight_for_a_listed_origin(make_settings: SettingsFactory) -> None:
    settings = make_settings(
        environment="test", database_url=UNREACHABLE_DATABASE, cors_origins=["https://a.example"]
    )
    with running(settings) as cors_client:
        preflight = cors_client.options(
            "/health/live",
            headers={"Origin": "https://a.example", "Access-Control-Request-Method": "GET"},
        )
        assert preflight.headers["access-control-allow-origin"] == "https://a.example"
        other = cors_client.get("/health/live", headers={"Origin": "https://evil.example"})
        assert "access-control-allow-origin" not in other.headers


def test_the_lifespan_configures_logging_and_disposes_the_engine(
    make_settings: SettingsFactory,
) -> None:
    application = create_app(make_settings(environment="test", database_url=UNREACHABLE_DATABASE))
    assert not hasattr(application.state, "engine")  # nothing is built at creation time
    with TestClient(application):
        assert application.state.engine is not None
        assert application.state.session_factory is not None
        assert logging.getLogger("uvicorn.access").disabled is True


def test_importing_the_module_does_not_build_the_app(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`fastapi run` reads `app.main:app`. A test can still import `create_app` without settings."""
    monkeypatch.chdir(tmp_path)  # no .env here
    for name in ("ENVIRONMENT", "DATABASE_URL", "JWT_SECRET", "WEBHOOK_SECRET"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    with pytest.raises(ValueError, match="environment"):  # pydantic's ValidationError
        _ = app.main.app
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("DATABASE_URL", UNREACHABLE_DATABASE)
    monkeypatch.setenv("JWT_SECRET", "j" * 40)
    monkeypatch.setenv("WEBHOOK_SECRET", "w" * 40)
    get_settings.cache_clear()
    assert isinstance(app.main.app, FastAPI)
    get_settings.cache_clear()


def test_an_unknown_module_attribute_is_an_attribute_error() -> None:
    with pytest.raises(AttributeError):
        _ = app.main.nothing_here

"""The request ID, the cache headers and the access event, on every response including the 500."""

import uuid
from collections.abc import Iterator

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from pydantic import BaseModel
from structlog.testing import capture_logs

from app.core.body_limit import BodyLimitMiddleware
from app.core.http import AccessLogMiddleware, NoStoreMiddleware, RequestIdMiddleware
from app.core.problem import install_exception_handlers
from app.core.routing import TolerantRouter

CAP = 1024
NO_STORE = {"cache-control": "no-store", "pragma": "no-cache"}


class Thing(BaseModel):
    name: str


def build_app() -> FastAPI:
    app = FastAPI(redirect_slashes=False)
    install_exception_handlers(app)
    router = TolerantRouter()

    @router.get("/ok")
    def ok() -> dict[str, str]:
        return {"ok": "1"}

    @router.get("/items/{item_id}")
    def item(item_id: uuid.UUID) -> dict[str, str]:
        return {"id": str(item_id)}

    @router.post("/things")
    def create_thing(thing: Thing) -> dict[str, str]:
        return {"name": thing.name}

    @router.get("/me")
    def me(request: Request) -> dict[str, str]:
        request.state.user_id = "user-42"  # what get_current_user does
        return {}

    @router.get("/crash")
    def crash() -> None:
        raise RuntimeError("unexpected")

    app.include_router(router)
    # Outermost first: request ID, access event, no-store headers, body limit.
    app.add_middleware(BodyLimitMiddleware, max_bytes=CAP)
    app.add_middleware(NoStoreMiddleware)
    app.add_middleware(AccessLogMiddleware)
    app.add_middleware(RequestIdMiddleware)
    return app


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(build_app(), raise_server_exceptions=False) as test_client:
        yield test_client


def request_events(logs: list[dict[str, object]]) -> list[dict[str, object]]:
    return [entry for entry in logs if entry["event"] == "http.request"]


# ---------------------------------------------------------------- request ID


def test_a_request_id_is_generated_when_the_client_sends_none(client: TestClient) -> None:
    request_id = client.get("/ok").headers["x-request-id"]
    assert str(uuid.UUID(request_id)) == request_id


@pytest.mark.parametrize("offered", ["abc12345", "trace-0123456789-abcdef", "A" * 64])
def test_a_well_formed_request_id_is_kept(client: TestClient, offered: str) -> None:
    assert client.get("/ok", headers={"X-Request-ID": offered}).headers["x-request-id"] == offered


@pytest.mark.parametrize("offered", ["abc", "a" * 65, "abc_12345", "abc 12345", "abc12345;drop"])
def test_a_malformed_request_id_is_replaced(client: TestClient, offered: str) -> None:
    returned = client.get("/ok", headers={"X-Request-ID": offered}).headers["x-request-id"]
    assert returned != offered
    uuid.UUID(returned)


def test_the_request_id_is_in_the_problem_body_and_the_header(client: TestClient) -> None:
    response = client.get("/items/not-a-uuid")
    assert response.status_code == 422
    assert response.json()["request_id"] == response.headers["x-request-id"]


# ---------------------------------------------------------------- cache headers


@pytest.mark.parametrize(
    ("method", "path", "status"),
    [
        ("GET", "/ok", 200),
        ("GET", "/nowhere", 404),
        ("DELETE", "/ok", 405),
        ("GET", "/items/not-a-uuid", 422),
    ],
)
def test_every_response_carries_no_store(
    client: TestClient, method: str, path: str, status: int
) -> None:
    response = client.request(method, path)
    assert response.status_code == status
    for name, value in NO_STORE.items():
        assert response.headers[name] == value


def test_a_413_carries_no_store(client: TestClient) -> None:
    response = client.post(
        "/things", content=b"x" * (CAP + 1), headers={"content-type": "text/plain"}
    )
    assert response.status_code == 413
    assert response.headers["cache-control"] == "no-store"


def test_the_catch_all_500_carries_no_store_and_the_request_id(client: TestClient) -> None:
    response = client.get("/crash")
    assert response.status_code == 500
    for name, value in NO_STORE.items():
        assert response.headers[name] == value
    assert response.headers["x-request-id"] == response.json()["request_id"]
    uuid.UUID(response.headers["x-request-id"])


# ---------------------------------------------------------------- access event


def test_one_event_per_request_with_the_route_template(client: TestClient) -> None:
    item_id = uuid.UUID(int=9)
    with capture_logs() as logs:
        response = client.get(f"/items/{item_id}", headers={"X-Request-ID": "trace-00000001"})
    (event,) = request_events(logs)
    assert event["method"] == "GET"
    assert event["route"] == "/items/{item_id}"  # the template, never the ID
    assert event["status"] == 200
    assert event["request_id"] == "trace-00000001" == response.headers["x-request-id"]
    assert isinstance(event["duration_ms"], float)
    assert str(item_id) not in str(event)


def test_the_query_string_never_reaches_the_log(client: TestClient) -> None:
    with capture_logs() as logs:
        client.get("/ok?email=jane.doe@example.in&token=abc")
    assert "jane.doe" not in str(logs)
    assert "token=abc" not in str(logs)


def test_the_user_id_is_logged_when_the_handler_sets_it(client: TestClient) -> None:
    with capture_logs() as logs:
        client.get("/me")
        client.get("/ok")
    with_user, without_user = request_events(logs)
    assert with_user["user_id"] == "user-42"
    assert without_user["user_id"] is None


def test_an_unknown_path_logs_no_route(client: TestClient) -> None:
    with capture_logs() as logs:
        client.get("/nowhere/123")
    (event,) = request_events(logs)
    assert event["status"] == 404
    assert event["route"] is None


def test_a_rejected_body_is_logged_too(client: TestClient) -> None:
    with capture_logs() as logs:
        client.post("/things", content=b"x" * (CAP + 1), headers={"content-type": "text/plain"})
    (event,) = request_events(logs)
    assert event["status"] == 413


def test_the_500_event_logs_status_500(client: TestClient) -> None:
    """The handler's response never reaches the middleware, so the middleware logs 500 itself."""
    with capture_logs() as logs:
        client.get("/crash")
    (event,) = request_events(logs)
    assert event["status"] == 500
    assert event["route"] == "/crash"
    assert any(entry["event"] == "unhandled_exception" for entry in logs)

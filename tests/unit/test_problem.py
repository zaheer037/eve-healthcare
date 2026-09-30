"""Every error renders as RFC 9457 problem JSON with the same fields (architecture §5.3)."""

from collections.abc import Iterator
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy.exc import DataError, InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeout
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core import errors
from app.core.problem import add_problem_schemas, install_exception_handlers, problems

REQUEST_ID = "req-test-0001"


class SetRequestId:
    """Stands in for RequestIdMiddleware: the handlers read the ID from `scope["state"]`."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            scope.setdefault("state", {})["request_id"] = REQUEST_ID
        await self.app(scope, receive, send)


class Thing(BaseModel):
    name: str
    age: int


def add_database_routes(router: APIRouter) -> None:
    """Routes that raise the database errors that the handlers know."""

    @router.get("/data-error")
    def data_error() -> None:
        raise DataError("INSERT ... (a@x.in)", {}, Exception("Key (email)=(a@x.in) is bad"))

    @router.get("/db-lock-timeout")
    def db_lock_timeout() -> None:
        pg_error = SimpleNamespace(sqlstate="55P03", diag=SimpleNamespace(constraint_name=None))
        raise OperationalError("UPDATE users ... (a@x.in)", {}, pg_error)  # type: ignore[arg-type]

    @router.get("/db-pool-timeout")
    def db_pool_timeout() -> None:
        raise PoolTimeout("QueuePool limit of size 10 overflow 10 reached, connection timed out")

    @router.get("/db-interface")
    def db_interface() -> None:
        raise InterfaceError(
            "SELECT 1", {}, RuntimeError("Key (email)=(a@x.in): connection closed")
        )


def build_app() -> FastAPI:
    app = FastAPI()
    install_exception_handlers(app)
    app.add_middleware(SetRequestId)
    router = APIRouter()

    @router.get("/app-error")
    def app_error() -> None:
        raise errors.BookingNotPayable("Booking 7c1f has status CANCELLED.")

    @router.get("/unauthenticated")
    def unauthenticated() -> None:
        raise errors.NotAuthenticated()

    @router.get("/rate-limited")
    def rate_limited() -> None:
        raise errors.RateLimited(retry_after=17)

    @router.get("/items/{item_id}")
    def item(item_id: UUID) -> dict[str, str]:
        return {"id": str(item_id)}

    @router.post("/things")
    def create_thing(thing: Thing) -> dict[str, str]:
        return {"name": thing.name}

    @router.get("/crash")
    def crash() -> None:
        raise RuntimeError("secret internal text: hunter2")

    @router.get("/multi")
    def multi_get() -> dict[str, str]:
        return {}

    @router.post("/multi")
    def multi_post() -> dict[str, str]:
        return {}

    add_database_routes(router)
    app.include_router(router)
    return app


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(build_app(), raise_server_exceptions=False) as test_client:
        yield test_client


def test_an_app_error_has_every_problem_field(client: TestClient) -> None:
    response = client.get("/app-error")
    assert response.status_code == 409
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json() == {
        "type": "urn:eve:problem:booking-not-payable",
        "title": "Booking is not payable",
        "status": 409,
        "detail": "Booking 7c1f has status CANCELLED.",
        "instance": "/app-error",
        "code": "BOOKING_NOT_PAYABLE",
        "request_id": REQUEST_ID,
    }


def test_a_401_carries_its_challenge(client: TestClient) -> None:
    response = client.get("/unauthenticated")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["code"] == "NOT_AUTHENTICATED"


def test_a_429_carries_retry_after(client: TestClient) -> None:
    response = client.get("/rate-limited")
    assert response.status_code == 429
    assert response.headers["retry-after"] == "17"


def test_a_malformed_uuid_is_a_422_with_the_field_name(client: TestClient) -> None:
    """AB15."""
    response = client.get("/items/not-a-uuid")
    body = response.json()
    assert response.status_code == 422
    assert body["code"] == "VALIDATION_ERROR"
    assert body["errors"][0]["field"] == "path.item_id"
    assert body["errors"][0]["type"] == "uuid_parsing"


def test_a_valid_uuid_passes(client: TestClient) -> None:
    assert client.get(f"/items/{UUID(int=1)}").status_code == 200


def test_body_errors_list_every_field_and_never_echo_the_input(client: TestClient) -> None:
    response = client.post("/things", json={"name": "hunter2-hunter2", "age": "not a number"})
    body = response.json()
    assert response.status_code == 422
    assert [item["field"] for item in body["errors"]] == ["body.age"]
    assert set(body["errors"][0]) == {"field", "message", "type"}
    assert "hunter2" not in response.text
    assert "input" not in response.text


def test_a_missing_field_is_reported(client: TestClient) -> None:
    body = client.post("/things", json={"age": 3}).json()
    assert [item["field"] for item in body["errors"]] == ["body.name"]


def test_malformed_json_is_a_422(client: TestClient) -> None:
    response = client.post(
        "/things", content=b'{"name": ', headers={"content-type": "application/json"}
    )
    assert response.status_code == 422
    assert response.json()["errors"][0]["type"] == "json_invalid"


def test_bytes_that_are_not_utf8_are_a_400(client: TestClient) -> None:
    response = client.post(
        "/things", content=b"\xff\xfe\x00", headers={"content-type": "application/json"}
    )
    assert response.status_code == 400
    assert response.json()["code"] == "BAD_REQUEST"


def test_an_unknown_path_is_a_404_problem(client: TestClient) -> None:
    response = client.get("/nowhere")
    assert response.status_code == 404
    assert response.json()["code"] == "ROUTE_NOT_FOUND"
    assert response.json()["instance"] == "/nowhere"


def test_a_405_lists_every_method_of_the_path(client: TestClient) -> None:
    response = client.put("/multi")
    assert response.status_code == 405
    assert response.json()["code"] == "METHOD_NOT_ALLOWED"
    assert response.headers["allow"] == "GET, POST"


def test_a_database_data_error_is_a_422_without_the_database_text(client: TestClient) -> None:
    response = client.get("/data-error")
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert "a@x.in" not in response.text
    assert "Key (email)" not in response.text


@pytest.mark.parametrize("path", ["/db-lock-timeout", "/db-pool-timeout", "/db-interface"])
def test_a_database_that_cannot_answer_is_a_503_that_asks_for_a_retry(
    client: TestClient, path: str
) -> None:
    """A lock timeout, a statement timeout, an exhausted pool and a lost connection all mean
    "try again". None of them shows the database text."""
    response = client.get(path)
    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "SERVICE_UNAVAILABLE"
    assert response.json()["request_id"] == REQUEST_ID
    for leak in ("a@x.in", "Key (email)", "QueuePool", "users"):
        assert leak not in response.text


def test_an_unexpected_error_is_a_500_with_no_internal_detail(client: TestClient) -> None:
    response = client.get("/crash")
    body = response.json()
    assert response.status_code == 500
    assert body["code"] == "INTERNAL_ERROR"
    assert "hunter2" not in response.text
    assert "secret internal text" not in response.text
    assert "detail" not in body


def test_the_500_sets_its_own_request_id_and_cache_headers(client: TestClient) -> None:
    """The 500 response bypasses the user middleware, so the handler sets these."""
    response = client.get("/crash")
    assert response.headers["x-request-id"] == REQUEST_ID
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert response.json()["request_id"] == REQUEST_ID


# ==== problems(): the `responses=` of a route


def test_problems_groups_codes_by_status_and_names_them() -> None:
    documented = problems(
        errors.NotAuthenticated,
        errors.InvalidToken,
        errors.Forbidden,
        errors.BookingNotFound,
        errors.DuplicateBooking,
        errors.BookingLimitReached,
    )
    assert sorted(documented) == [401, 403, 404, 409]
    assert documented[401]["description"] == "One of `NOT_AUTHENTICATED`, `INVALID_TOKEN`."
    assert documented[403]["description"] == "`FORBIDDEN`."
    assert documented[409]["description"] == "One of `DUPLICATE_BOOKING`, `BOOKING_LIMIT_REACHED`."


def test_every_problem_response_is_problem_json_with_the_problem_model() -> None:
    for entry in problems(errors.BookingNotFound, errors.PayloadTooLarge).values():
        assert entry["content"] == {
            "application/problem+json": {"schema": {"$ref": "#/components/schemas/ProblemDetail"}}
        }


def test_problems_of_nothing_is_nothing() -> None:
    assert problems() == {}


def test_the_problem_model_and_the_models_under_it_are_added_to_a_document() -> None:
    document: dict[str, object] = {"openapi": "3.1.0"}
    add_problem_schemas(document)
    schemas = document["components"]["schemas"]  # type: ignore[index]
    assert {"ProblemDetail", "ErrorCode", "FieldError"} <= set(schemas)
    assert "$defs" not in schemas["ProblemDetail"]
    assert schemas["ProblemDetail"]["properties"]["code"] == {
        "$ref": "#/components/schemas/ErrorCode"
    }

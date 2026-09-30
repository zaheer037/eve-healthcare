"""TolerantRouter: both slash spellings, HEAD next to GET, no redirect, canonical OpenAPI."""

from collections.abc import Iterator
from uuid import UUID

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from app.core.problem import install_exception_handlers
from app.core.routing import TolerantRouter


def build_app() -> FastAPI:
    app = FastAPI(redirect_slashes=False)
    install_exception_handlers(app)
    payments = TolerantRouter(prefix="/payments")

    @payments.post("/", status_code=201)
    def create_payment() -> dict[str, str]:
        return {"route": "create"}

    @payments.post("/webhook/")
    def webhook() -> dict[str, str]:
        return {"route": "webhook"}

    @payments.get("/{payment_id}")
    def get_payment(payment_id: UUID) -> dict[str, str]:
        return {"id": str(payment_id)}

    bookings = TolerantRouter(prefix="/bookings")

    @bookings.get("/")
    def list_bookings() -> list[str]:
        return []

    @bookings.post("/", status_code=201)
    def create_booking() -> dict[str, str]:
        return {}

    app.include_router(payments)
    app.include_router(bookings)
    return app


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(build_app(), follow_redirects=False) as test_client:
        yield test_client


@pytest.mark.parametrize("path", ["/payments", "/payments/"])
def test_a_collection_answers_in_both_spellings(client: TestClient, path: str) -> None:
    response = client.post(path)
    assert response.status_code == 201
    assert response.json() == {"route": "create"}


@pytest.mark.parametrize("path", ["/payments/webhook", "/payments/webhook/"])
def test_the_webhook_answers_in_both_spellings(client: TestClient, path: str) -> None:
    response = client.post(path)
    assert response.status_code == 200
    assert response.json() == {"route": "webhook"}


@pytest.mark.parametrize("suffix", ["", "/"])
def test_an_item_answers_in_both_spellings(client: TestClient, suffix: str) -> None:
    assert client.get(f"/payments/{UUID(int=7)}{suffix}").status_code == 200


@pytest.mark.parametrize("path", ["/bookings", "/bookings/"])
def test_head_answers_wherever_get_does(client: TestClient, path: str) -> None:
    response = client.head(path)
    assert response.status_code == 200
    assert response.content == b""


def test_openapi_lists_only_the_canonical_spelling_and_no_head(client: TestClient) -> None:
    paths = client.app.openapi()["paths"]  # type: ignore[attr-defined]
    assert sorted(paths) == [
        "/bookings/",
        "/payments/",
        "/payments/webhook/",
        "/payments/{payment_id}",
    ]
    assert all("head" not in operations for operations in paths.values())


def test_a_405_lists_every_method_the_path_serves(client: TestClient) -> None:
    for path in ("/bookings/", "/bookings"):
        response = client.put(path)
        assert response.status_code == 405
        assert response.headers["allow"] == "GET, HEAD, POST"


def test_a_plain_router_redirects_and_refuses_head() -> None:
    """The control: the default router is why TolerantRouter exists (spec check O)."""
    app = FastAPI()
    plain = APIRouter(prefix="/auth")
    plain.post("/signup", status_code=201)(lambda: {"ok": 1})
    plain.get("/status")(lambda: {"ok": 1})
    app.include_router(plain)
    with TestClient(app, follow_redirects=False) as control:
        assert control.post("/auth/signup/").status_code == 307
        assert control.head("/auth/status").status_code == 405

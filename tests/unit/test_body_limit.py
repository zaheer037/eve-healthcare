"""BodyLimitMiddleware: one cap and a problem+json 413, chunked or not (AB16)."""

import asyncio
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel
from starlette.types import Message, Receive, Scope, Send

from app.core.body_limit import BodyLimitMiddleware
from app.core.http import RequestIdMiddleware
from app.core.problem import install_exception_handlers

CAP = 1024
JSON = {"content-type": "application/json"}
PROBLEM_FIELDS = {"type", "title", "status", "code", "instance", "request_id"}


class Payload(BaseModel):
    text: str


def build_app(handler_calls: list[str]) -> FastAPI:
    app = FastAPI()
    install_exception_handlers(app)

    @app.post("/echo")
    def echo(payload: Payload) -> dict[str, int]:
        handler_calls.append("echo")
        return {"length": len(payload.text)}

    @app.post("/no-body")  # a route that ignores the body but has a side effect
    def no_body() -> dict[str, str]:
        handler_calls.append("no-body")
        return {"ok": "1"}

    app.add_middleware(BodyLimitMiddleware, max_bytes=CAP)
    app.add_middleware(RequestIdMiddleware)
    return app


@pytest.fixture
def handler_calls() -> list[str]:
    return []


@pytest.fixture
def client(handler_calls: list[str]) -> Iterator[TestClient]:
    with TestClient(build_app(handler_calls)) as test_client:
        yield test_client


def chunks(*sizes: int) -> Iterator[bytes]:
    yield from (b"y" * size for size in sizes)


def test_a_content_length_over_the_cap_gets_a_413_and_the_handler_never_runs(
    client: TestClient, handler_calls: list[str]
) -> None:
    response = client.post("/no-body", content=b"x" * (CAP + 1), headers=JSON)
    body = response.json()
    assert response.status_code == 413
    assert response.headers["content-type"] == "application/problem+json"
    assert set(body) == PROBLEM_FIELDS
    assert body["code"] == "PAYLOAD_TOO_LARGE"
    assert body["request_id"] == response.headers["x-request-id"]
    assert handler_calls == []


def test_a_chunked_body_over_the_cap_gets_a_413_even_on_a_route_that_ignores_it(
    client: TestClient, handler_calls: list[str]
) -> None:
    response = client.post("/no-body", content=chunks(600, 600), headers=JSON)
    assert response.status_code == 413
    assert set(response.json()) == PROBLEM_FIELDS
    assert handler_calls == []


def test_a_body_of_exactly_the_cap_passes(client: TestClient) -> None:
    assert client.post("/no-body", content=b"x" * CAP, headers=JSON).status_code == 200


def test_a_small_body_passes_and_reaches_the_handler(
    client: TestClient, handler_calls: list[str]
) -> None:
    response = client.post("/echo", json={"text": "hello"})
    assert response.status_code == 200
    assert response.json() == {"length": 5}
    assert handler_calls == ["echo"]


def test_a_chunked_body_under_the_cap_is_replayed_to_the_handler(client: TestClient) -> None:
    body = b'{"text": "' + b"a" * 300 + b'"}'
    pieces = (body[:100], body[100:200], body[200:])
    response = client.post("/echo", content=iter(pieces), headers=JSON)
    assert response.status_code == 200
    assert response.json() == {"length": 300}


async def call_asgi(
    app: BodyLimitMiddleware, scope: Scope, first_message: Message
) -> list[Message]:
    sent: list[Message] = []

    async def receive() -> Message:
        return first_message

    async def send(message: Message) -> None:
        sent.append(message)

    await app(scope, receive, send)
    return sent


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param([(b"content-length", b"12abc")], id="malformed-content-length"),
        # RFC 9112 section 6.1 lets a server refuse this. The h11 parser passes both headers on,
        # and the small length would hide a chunked body of any size from the cap.
        pytest.param(
            [(b"content-length", b"5"), (b"transfer-encoding", b"chunked")], id="both-framings"
        ),
    ],
)
def test_a_request_with_broken_framing_is_a_400(headers: list[tuple[bytes, bytes]]) -> None:
    async def never(scope: Scope, receive: Receive, send: Send) -> None:
        raise AssertionError("the application must not run")

    scope: Scope = {"type": "http", "method": "POST", "path": "/echo", "headers": headers}
    middleware = BodyLimitMiddleware(never, max_bytes=CAP)
    sent = asyncio.run(call_asgi(middleware, scope, {"type": "http.request"}))
    assert sent[0]["status"] == 400


def test_a_websocket_scope_passes_through() -> None:
    seen: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    middleware = BodyLimitMiddleware(app, max_bytes=CAP)
    asyncio.run(
        call_asgi(middleware, {"type": "websocket", "headers": []}, {"type": "websocket.connect"})
    )
    assert seen == ["websocket"]

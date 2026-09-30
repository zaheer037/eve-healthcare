"""Pure ASGI middleware: the request ID, the cache headers and the access event.

They are pure ASGI, not `BaseHTTPMiddleware`, so they do not buffer bodies and do not break
context propagation. The request context lives in `scope["state"]`, not in a contextvar: a
contextvar that a middleware binds is cleared before the catch-all 500 handler runs (sharp edge 48).
"""

import re
import time
import uuid

import structlog
from fastapi import Request, Response
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

NO_STORE_HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache"}
REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9-]{8,64}")

log = structlog.get_logger()


def located(request: Request, response: Response, route_name: str, **path_params: object) -> None:
    """Set `Location` to a path. `url_path_for` returns no host, so a hostile Host header cannot
    reach it, and a later `/v1` prefix needs no change here (sharp edge 47)."""
    response.headers["Location"] = str(request.app.url_path_for(route_name, **path_params))


class RequestIdMiddleware:
    """Accept a well-formed `X-Request-ID`, or make one. Store it in `scope["state"]`."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        offered = Headers(scope=scope).get("x-request-id", "")
        request_id = offered if REQUEST_ID_PATTERN.fullmatch(offered) else str(uuid.uuid4())
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        await self.app(scope, receive, send_with_request_id)


class NoStoreMiddleware:
    """Put `Cache-Control: no-store` and `Pragma: no-cache` on every response that passes through.

    RFC 6749 section 5.1 asks for both on token responses. A header set inside a route reaches
    only the 200 (sharp edge 45). The catch-all 500 response bypasses this middleware, so its
    handler sets the same headers (sharp edge 51).
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_no_store(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in NO_STORE_HEADERS.items():
                    headers[name] = value
            await send(message)

        await self.app(scope, receive, send_no_store)


class AccessLogMiddleware:
    """Log one `http.request` event per request: method, route template, status and duration.

    It never logs the query string. Uvicorn's own access log is off for that reason
    (`configure_logging`). The status is 500 when the app raises, because the response of the
    catch-all handler never passes through this middleware.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status = 500  # stays 500 when the app raises before it starts a response

        async def capture_status(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, capture_status)
        finally:
            state = scope.get("state", {})
            log.info(
                "http.request",
                method=scope["method"],
                route=getattr(scope.get("route"), "path", None),
                status=status,
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
                request_id=state.get("request_id"),
                user_id=state.get("user_id"),
            )

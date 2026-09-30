"""One cap on the size of every request body, answered as a problem+json 413."""

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core import errors
from app.core.errors import AppError
from app.core.problem import problem_response


class BodyLimitMiddleware:
    """Reject a body over `max_bytes` before the application runs.

    A `Content-Length` header decides at once. A chunked body has no length, so the middleware
    reads at most `max_bytes` into memory and replays it to the application. Starlette's own
    middleware answers `text/plain` instead, and for a route without a body it runs the handler
    first (architecture §2.3).
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = Headers(scope=scope).get("content-length")
        if declared is not None:
            if not declared.isdecimal():
                await self._reject(errors.BadRequest(), scope, receive, send)
            elif int(declared) > self.max_bytes:
                await self._reject(errors.PayloadTooLarge(), scope, receive, send)
            else:
                await self.app(scope, receive, send)
            return
        await self._read_chunked(scope, receive, send)

    async def _read_chunked(self, scope: Scope, receive: Receive, send: Send) -> None:
        chunks: list[Message] = []
        received = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":  # the client went away
                return
            chunks.append(message)
            received += len(message.get("body", b""))
            if received > self.max_bytes:
                await self._reject(errors.PayloadTooLarge(), scope, receive, send)
                return
            if not message.get("more_body", False):
                break
        replay = iter(chunks)

        async def replaying_receive() -> Message:
            message = next(replay, None)
            return message if message is not None else await receive()

        await self.app(scope, replaying_receive, send)

    @staticmethod
    async def _reject(error: AppError, scope: Scope, receive: Receive, send: Send) -> None:
        await problem_response(error, scope)(scope, receive, send)

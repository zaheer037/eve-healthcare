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

    A request that names both a length and a chunked encoding is refused (RFC 9112 section 6.1).
    The h11 parser passes both headers on, and a small `Content-Length` would hide a chunked body
    of any size from the cap. The httptools parser refuses the request before it gets here.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        declared = headers.get("content-length")
        if declared is None:
            await self._read_chunked(scope, receive, send)
        elif "transfer-encoding" in headers:
            error = errors.BadRequest("Send Content-Length or Transfer-Encoding, not both.")
            await self._reject(error, scope, receive, send)
        elif not declared.isdecimal():
            error = errors.BadRequest("Content-Length must be a whole number.")
            await self._reject(error, scope, receive, send)
        elif int(declared) > self.max_bytes:
            await self._reject(errors.PayloadTooLarge(), scope, receive, send)
        else:
            await self.app(scope, receive, send)

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

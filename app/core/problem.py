"""RFC 9457 problem responses: one format for every error (architecture §5.3)."""

from http import HTTPStatus
from typing import Any, cast

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.exc import DataError, InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeout
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.routing import Match
from starlette.types import Scope

from app.core import errors
from app.core.errors import ERROR_SPECS, AppError, ErrorCode
from app.core.http import NO_STORE_HEADERS

log = structlog.get_logger()

PROBLEM_MEDIA_TYPE = "application/problem+json"
SERVED_METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE")


class FieldError(BaseModel):
    field: str  # a dotted path such as "body.appointment_at"
    message: str
    type: str


class ProblemDetail(BaseModel):
    """The body of every error response. It is also the model behind every `responses=` entry."""

    type: str
    title: str
    status: int
    detail: str | None = None
    instance: str
    code: ErrorCode
    request_id: str | None = None
    errors: list[FieldError] | None = None


def describe(codes: list[ErrorCode]) -> str:
    """ "`FORBIDDEN`." for one code and "One of `A`, `B`." for several."""
    listed = ", ".join(f"`{code}`" for code in codes)
    return f"{listed}." if len(codes) == 1 else f"One of {listed}."


def problems(*errors: type[AppError]) -> dict[int | str, dict[str, Any]]:
    """The `responses=` of a route for the errors that it can answer, one entry per status.

    Each description names the codes that cause the status, so `/docs` tells a client what to
    switch on. The body is the problem model and the media type is `application/problem+json`. The
    model itself is added to the document by `add_problem_schemas`.
    """
    codes_by_status: dict[int, list[ErrorCode]] = {}
    for error in errors:
        codes_by_status.setdefault(ERROR_SPECS[error.code].status, []).append(error.code)
    return {
        status: {
            "description": describe(codes),
            "content": {
                PROBLEM_MEDIA_TYPE: {"schema": {"$ref": "#/components/schemas/ProblemDetail"}}
            },
        }
        for status, codes in codes_by_status.items()
    }


def add_problem_schemas(schema: dict[str, Any]) -> None:
    """Register `ProblemDetail` and the models under it in an OpenAPI document.

    A response that names its media type by hand, as `problems()` does, does not register the
    model, and FastAPI would only offer `application/json` for a `model=` entry.
    """
    models = ProblemDetail.model_json_schema(ref_template="#/components/schemas/{model}")
    definitions = models.pop("$defs", {})
    schemas = schema.setdefault("components", {}).setdefault("schemas", {})
    schemas.update({"ProblemDetail": models, **definitions})


def request_id_of(scope: Scope) -> str | None:
    request_id: str | None = scope.get("state", {}).get("request_id")
    return request_id


def problem_response(
    error: AppError,
    scope: Scope,
    *,
    errors: list[FieldError] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = ProblemDetail(
        type=error.type_urn,
        title=error.title,
        status=error.status_code,
        detail=error.detail,
        instance=scope["path"],
        code=error.code,
        request_id=request_id_of(scope),
        errors=errors,
    )
    return JSONResponse(
        body.model_dump(mode="json", exclude_none=True),
        status_code=error.status_code,
        media_type=PROBLEM_MEDIA_TYPE,
        headers={**error.headers(), **(headers or {})},
    )


def allowed_methods(request: Request) -> str:
    """The `Allow` value of a 405 (RFC 9110 section 15.5.6): every method that the path serves.

    Starlette lists the methods of the first matching route only. This asks each route whether it
    fully matches the path with each served method. It uses only Starlette's route protocol,
    because FastAPI 0.137 and newer keep included routers behind a private wrapper, so the
    app's route list is not flat.
    """
    supported = []
    for method in SERVED_METHODS:
        probe = {**request.scope, "method": method}
        if any(route.matches(probe)[0] == Match.FULL for route in request.app.router.routes):
            supported.append(method)
    return ", ".join(supported)


async def handle_app_error(request: Request, exc: Exception) -> JSONResponse:
    return problem_response(cast(AppError, exc), request.scope)


_HTTP_STATUS_ERRORS: dict[int, type[AppError]] = {
    HTTPStatus.BAD_REQUEST: errors.BadRequest,
    HTTPStatus.NOT_FOUND: errors.RouteNotFound,
    HTTPStatus.METHOD_NOT_ALLOWED: errors.MethodNotAllowed,
}


async def handle_http_exception(request: Request, exc: Exception) -> JSONResponse:
    """Starlette raises these itself: an unreadable body (400), an unknown path, a wrong method."""
    status = cast(StarletteHTTPException, exc).status_code
    error_class = _HTTP_STATUS_ERRORS.get(status)
    if error_class is None:  # defensive: the application never raises another status on purpose
        log.warning("http_exception_unmapped", status=status)
        error_class = (
            errors.InternalError
            if status >= HTTPStatus.INTERNAL_SERVER_ERROR
            else errors.BadRequest
        )
    headers = (
        {"Allow": allowed_methods(request)} if status == HTTPStatus.METHOD_NOT_ALLOWED else None
    )
    return problem_response(error_class(), request.scope, headers=headers)


async def handle_validation_error(request: Request, exc: Exception) -> JSONResponse:
    """Keep the location, message and type. `input` and `ctx` can hold a password, so drop them."""
    field_errors = [
        FieldError(
            field=".".join(str(part) for part in item["loc"]),
            message=item["msg"],
            type=item["type"],
        )
        for item in cast(RequestValidationError, exc).errors()
    ]
    return problem_response(errors.ValidationFailed(), request.scope, errors=field_errors)


async def handle_data_error(request: Request, exc: Exception) -> JSONResponse:
    """A value outside a column type. PostgreSQL text echoes row values, so it stays out."""
    log.warning(
        "database_data_error", sqlstate=getattr(cast(DataError, exc).orig, "sqlstate", None)
    )
    return problem_response(
        errors.ValidationFailed("A value is outside the range that the database accepts."),
        request.scope,
    )


async def handle_database_unavailable(request: Request, exc: Exception) -> JSONResponse:
    """The database cannot answer now: a lock or statement timeout, a deadlock, a lost connection
    or an exhausted pool. All of them mean "try again", so the client gets a 503 and a retry hint.
    PostgreSQL text echoes row values, so it stays out of the response and the log."""
    log.warning(
        "database_unavailable",
        error=type(exc).__name__,
        sqlstate=getattr(getattr(exc, "orig", None), "sqlstate", None),
    )
    return problem_response(
        errors.ServiceUnavailable("The service is busy. Try again shortly."), request.scope
    )


async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
    """The catch-all 500.

    Starlette sends this response from `ServerErrorMiddleware`, outside every user middleware.
    So the handler sets the request ID and the cache headers itself.
    """
    request_id = request_id_of(request.scope)
    log.error(
        "unhandled_exception",
        exc_info=exc,
        request_id=request_id,
        method=request.method,
        path=request.url.path,
    )
    headers = dict(NO_STORE_HEADERS)
    if request_id is not None:
        headers["X-Request-ID"] = request_id
    return problem_response(errors.InternalError(), request.scope, headers=headers)


def install_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, handle_app_error)
    app.add_exception_handler(StarletteHTTPException, handle_http_exception)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(DataError, handle_data_error)
    for unavailable in (OperationalError, InterfaceError, PoolTimeout):
        app.add_exception_handler(unavailable, handle_database_unavailable)
    app.add_exception_handler(Exception, handle_unexpected)

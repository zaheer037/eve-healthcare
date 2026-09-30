"""The application factory: settings, lifespan, middleware, error handlers and routers."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib import metadata
from typing import Any

import structlog
from fastapi import Depends, FastAPI
from fastapi.routing import APIRoute
from starlette.middleware.cors import CORSMiddleware

from app.auth.router import auth_router, users_router
from app.bookings.router import router as bookings_router
from app.catalog.router import centres_admin, centres_public, tests_admin, tests_public
from app.core import health
from app.core.body_limit import BodyLimitMiddleware
from app.core.config import Settings, get_settings
from app.core.db import create_db_engine, create_session_factory
from app.core.errors import BadRequest, InternalError, PayloadTooLarge, ServiceUnavailable
from app.core.http import (
    AccessLogMiddleware,
    NoStoreMiddleware,
    RequestIdMiddleware,
    bind_request_id,
)
from app.core.logging import configure_logging
from app.core.problem import add_problem_schemas, install_exception_handlers, problems
from app.core.security import HashSlots, dummy_hash
from app.core.throttle import LoginThrottle
from app.payments.router import router as payments_router

log = structlog.get_logger()

DESCRIPTION = """
Book diagnostic tests at centres, pay for them with a simulated provider, and receive the
provider's payment events by webhook.

**Sign in.** Sign up at `POST /auth/signup`, then press **Authorize** and enter the email and the
password. Swagger sends them to `POST /auth/token`. Any client can use `POST /auth/login` instead
and send the token as `Authorization: Bearer <token>`.

**Errors.** Every error is `application/problem+json` (RFC 9457). Switch on `code`, not on the
text. The four statuses that any operation can answer (`400`, `413`, `500`, `503`) are listed once
here and repeated on every operation.

**Retries.** `GET`, `PUT`, `PATCH` and `DELETE` are idempotent. `POST /payments/` returns the live
payment of a booking again, and the webhook answers `duplicate` for an event that it has stored.
"""

TAGS = [
    {"name": "auth", "description": "Sign up, log in and get an access token."},
    {"name": "users", "description": "The signed-in user."},
    {"name": "centres", "description": "Diagnostic centres and the price of each test there."},
    {"name": "tests", "description": "The catalogue of diagnostic tests."},
    {"name": "bookings", "description": "Book a test, list and read your bookings, cancel one."},
    {"name": "payments", "description": "Pay a booking, read a payment, receive provider events."},
    {"name": "health", "description": "Probes for the orchestrator."},
]

# What any operation can answer, listed once (spec section 7.2).
SHARED_PROBLEMS = problems(BadRequest, PayloadTooLarge, InternalError, ServiceUnavailable)


def operation_id(route: APIRoute) -> str:
    """The function name, so a client generator gets `create_booking` and not a path-shaped id."""
    return route.name


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    # Logging is configured here, not at import time: Uvicorn applies its own logging
    # configuration after it imports the application module (spec check X).
    configure_logging(settings)
    engine = create_db_engine(settings)
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)
    app.state.hash_slots = HashSlots(settings.hash_max_concurrency)
    app.state.throttle = LoginThrottle(settings)  # counters start empty with every app
    app.state.dummy_hash = dummy_hash()
    log.info("app.started", environment=settings.environment)
    try:
        yield
    finally:
        engine.dispose()
        log.info("app.stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    docs = settings.docs_enabled
    app = FastAPI(
        title="EVE Healthcare API",
        version=metadata.version("eve-healthcare-api"),
        summary="Diagnostic-test bookings, simulated payments and an idempotent payment webhook.",
        description=DESCRIPTION.strip(),
        openapi_tags=TAGS,
        responses=SHARED_PROBLEMS,
        generate_unique_id_function=operation_id,
        lifespan=lifespan,
        redirect_slashes=False,  # TolerantRouter serves the other spelling (spec section 4.3)
        dependencies=[Depends(bind_request_id, scope="function")],
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
    )
    app.state.settings = settings
    install_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(auth_router)
    app.include_router(users_router)
    app.include_router(centres_public)
    app.include_router(centres_admin)
    app.include_router(tests_public)
    app.include_router(tests_admin)
    app.include_router(bookings_router)
    app.include_router(payments_router)

    # Starlette wraps in reverse order, so the last middleware added runs first. The order,
    # outermost first: request ID, access event, no-store headers, CORS, body limit. The access
    # event sits just inside the request ID, so it also logs the 413 that the body limit answers.
    app.add_middleware(BodyLimitMiddleware, max_bytes=settings.request_body_limit_bytes)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
            expose_headers=["X-Request-ID", "Retry-After"],
        )
    app.add_middleware(NoStoreMiddleware)
    app.add_middleware(AccessLogMiddleware)
    app.add_middleware(RequestIdMiddleware)
    build_openapi = app.openapi

    def openapi_with_problems() -> dict[str, Any]:
        first_time = app.openapi_schema is None
        schema = build_openapi()  # builds and caches the document
        if first_time:
            add_problem_schemas(schema)
        return schema

    app.openapi = openapi_with_problems  # type: ignore[method-assign]
    return app


def __getattr__(name: str) -> FastAPI:
    """`fastapi run` reads `app.main:app`. The app is built on first access, not at import, so a
    test can import `create_app` without a complete environment."""
    if name == "app":
        return create_app()
    raise AttributeError(name)

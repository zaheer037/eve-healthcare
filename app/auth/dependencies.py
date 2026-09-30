"""Who is calling: the current user, the admin check and the gate for the hashing endpoints."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, Form, Request
from fastapi.exceptions import RequestValidationError
from fastapi.security import OAuth2PasswordBearer
from pydantic import SecretStr

from app.auth.repository import UserRepository
from app.auth.schemas import LoginRequest, SignupRequest, TokenForm
from app.core.config import Settings
from app.core.db import SessionDep
from app.core.errors import Forbidden, InvalidToken, NotAuthenticated
from app.core.security import HashSlots, decode_access_token
from app.core.throttle import LoginThrottle
from app.models.user import Role, User

# auto_error=False: a missing header must give our problem response, not FastAPI's own.
# The scheme also makes Swagger UI show the Authorize button (ADR-08).
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token", auto_error=False)


def get_current_user(
    request: Request,
    session: SessionDep,
    token: Annotated[str | None, Depends(oauth2_scheme)],
) -> User:
    """Decode the token and load the user: one primary-key read per request.

    The role comes from the database and not from the token, so a change takes effect at once.
    """
    if not token:  # no header, another scheme, or `Bearer` with nothing after it
        raise NotAuthenticated
    settings: Settings = request.app.state.settings
    user = UserRepository(session).get(decode_access_token(token, settings))
    if user is None:  # a valid token for a user that no longer exists
        raise InvalidToken
    request.state.user_id = str(user.id)  # the access event logs it
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_admin(user: CurrentUser) -> User:
    if user.role is not Role.ADMIN:
        raise Forbidden("This action needs an administrator.")
    return user


def client_ip(request: Request) -> str:
    """The address that Uvicorn's proxy-header handling puts in the scope (architecture §7.2)."""
    return request.client.host if request.client else "unknown"


@asynccontextmanager
async def auth_gate(request: Request, email: str | None = None) -> AsyncIterator[None]:
    """Throttle the request, then hold a hashing slot until the handler returns.

    It runs on the event loop, before the request needs a pool thread. The throttle comes first,
    so a client that must back off hears 429 and not 503. Nothing here waits.
    """
    throttle: LoginThrottle = request.app.state.throttle
    slots: HashSlots = request.app.state.hash_slots
    throttle.check(client_ip(request), email)
    with slots.slot():
        yield


def check_password_length(password: SecretStr, minimum: int) -> None:
    """Enforce PASSWORD_MIN_LENGTH. The schema cannot, because the minimum is a setting.

    The error has the same shape as a schema error, so the client sees an ordinary 422.
    """
    if len(password.get_secret_value()) < minimum:
        raise RequestValidationError(
            [
                {
                    "type": "string_too_short",
                    "loc": ("body", "password"),
                    "msg": f"String should have at least {minimum} characters",
                    "ctx": {"min_length": minimum},
                }
            ]
        )


# The three gates share `auth_gate`. Each one returns the parsed input, so the handler does not
# declare the body a second time. The slot is held until the handler returns (scope="function").


async def signup_gate(request: Request, data: SignupRequest) -> AsyncIterator[SignupRequest]:
    settings: Settings = request.app.state.settings
    check_password_length(data.password, settings.password_min_length)
    async with auth_gate(request):  # a signup counts against the IP limit only
        yield data


async def login_gate(request: Request, credentials: LoginRequest) -> AsyncIterator[LoginRequest]:
    async with auth_gate(request, credentials.email):
        yield credentials


async def token_gate(
    request: Request, form: Annotated[TokenForm, Form()]
) -> AsyncIterator[TokenForm]:
    async with auth_gate(request, form.username):
        yield form


SignupInput = Annotated[SignupRequest, Depends(signup_gate, scope="function")]
LoginInput = Annotated[LoginRequest, Depends(login_gate, scope="function")]
TokenInput = Annotated[TokenForm, Depends(token_gate, scope="function")]

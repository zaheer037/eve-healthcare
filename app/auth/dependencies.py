"""Who is calling: the current user, the admin check and the gate for the hashing endpoints."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import OAuth2PasswordBearer

from app.auth.repository import UserRepository
from app.core.config import Settings
from app.core.db import SessionDep
from app.core.errors import Forbidden, InvalidToken, NotAuthenticated
from app.core.security import HashSlots, decode_access_token
from app.core.throttle import LoginThrottle
from app.models.user import Role, User

# auto_error=False: a missing header must give our problem response, not FastAPI's own.
# The scheme also makes Swagger UI show the Authorize button (decision D4).
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
    """The address that Uvicorn's proxy-header handling puts in the scope (spec section 9.2)."""
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

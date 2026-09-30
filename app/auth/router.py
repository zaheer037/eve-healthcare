"""Signup, login, token and the current user (spec section 7.4)."""

from typing import Annotated

from fastapi import Depends, Request

from app.auth.dependencies import CurrentUser, LoginInput, SignupInput, TokenInput
from app.auth.schemas import TokenRead, UserRead
from app.auth.service import AuthService
from app.core.db import SessionDep
from app.core.errors import (
    EmailAlreadyRegistered,
    InvalidCredentials,
    InvalidToken,
    NotAuthenticated,
    RateLimited,
    ValidationFailed,
)
from app.core.problem import problems
from app.core.routing import TolerantRouter
from app.models.user import User

auth_router = TolerantRouter(prefix="/auth", tags=["auth"])
users_router = TolerantRouter(prefix="/users", tags=["users"])


def get_auth_service(request: Request, session: SessionDep) -> AuthService:
    state = request.app.state
    return AuthService(session, state.settings, state.throttle, state.dummy_hash)


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]


@auth_router.post(
    "/signup",
    response_model=UserRead,
    status_code=201,
    summary="Create a patient account",
    responses=problems(EmailAlreadyRegistered, ValidationFailed, RateLimited),
)
def signup(data: SignupInput, service: AuthServiceDep) -> User:
    return service.signup(
        email=data.email,
        password=data.password.get_secret_value(),
        full_name=data.full_name,
        phone=data.phone,
    )


@auth_router.post(
    "/login",
    summary="Log in with JSON and receive an access token",
    responses=problems(InvalidCredentials, ValidationFailed, RateLimited),
)
def login(credentials: LoginInput, service: AuthServiceDep) -> TokenRead:
    token, expires_in = service.login(credentials.email, credentials.password.get_secret_value())
    return TokenRead(access_token=token, expires_in=expires_in)


@auth_router.post(
    "/token",
    summary="Log in with the OAuth2 password form (for Swagger's Authorize button)",
    responses=problems(InvalidCredentials, ValidationFailed, RateLimited),
)
def token(form: TokenInput, service: AuthServiceDep) -> TokenRead:
    access_token, expires_in = service.login(form.username, form.password.get_secret_value())
    return TokenRead(access_token=access_token, expires_in=expires_in)


@users_router.get(
    "/me",
    response_model=UserRead,
    summary="The signed-in user",
    responses=problems(NotAuthenticated, InvalidToken),
)
def read_me(user: CurrentUser) -> User:
    return user

"""Signup, login, token and the current user (spec section 7.4)."""

from typing import Annotated

from fastapi import Depends, Request

from app.auth.dependencies import CurrentUser, LoginInput, SignupInput, TokenInput
from app.auth.schemas import TokenRead, UserRead
from app.auth.service import AuthService
from app.core.db import SessionDep
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
)
def signup(data: SignupInput, service: AuthServiceDep) -> User:
    return service.signup(data)


@auth_router.post("/login", summary="Log in with JSON and receive an access token")
def login(credentials: LoginInput, service: AuthServiceDep) -> TokenRead:
    return service.login(credentials.email, credentials.password.get_secret_value())


@auth_router.post(
    "/token",
    summary="Log in with the OAuth2 password form (for Swagger's Authorize button)",
)
def token(form: TokenInput, service: AuthServiceDep) -> TokenRead:
    return service.login(form.username, form.password.get_secret_value())


@users_router.get("/me", response_model=UserRead, summary="The signed-in user")
def read_me(user: CurrentUser) -> User:
    return user

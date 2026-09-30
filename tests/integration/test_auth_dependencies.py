"""get_current_user and require_admin, on routes that only this test mounts (AB1, AB14)."""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import jwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.orm import Session
from structlog.testing import capture_logs

from app.auth.dependencies import CurrentUser, require_admin
from app.core.config import Settings
from app.core.routing import TolerantRouter
from app.core.security import create_access_token
from app.main import create_app
from app.models.user import Role, User
from tests.factories import make_user
from tests.support import AUTH_LOOKUP, QueryCounter


@pytest.fixture
def protected_client(test_settings: Settings) -> Iterator[TestClient]:
    application = create_app(test_settings)
    router = TolerantRouter()

    @router.get("/whoami")
    def whoami(user: CurrentUser) -> dict[str, str]:
        return {"id": str(user.id), "role": user.role}

    @router.get("/admin-only", dependencies=[Depends(require_admin)])
    def admin_only() -> dict[str, str]:
        return {"ok": "1"}

    application.include_router(router)
    with TestClient(application, raise_server_exceptions=False) as client:
        yield client


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def token_for(user: User, settings: Settings) -> str:
    return create_access_token(user.id, settings)[0]


def signed(settings: Settings, **claims: Any) -> str:
    """A correctly signed token with the claims of a real one, then the given changes."""
    now = datetime.now(UTC)
    base: dict[str, Any] = {
        "sub": str(uuid.uuid4()),
        "iat": now,
        "exp": now + timedelta(minutes=30),
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
    }
    return jwt.encode({**base, **claims}, settings.jwt_secret.get_secret_value(), algorithm="HS256")


# ---------------------------------------------------------------- 401 not authenticated


def test_a_missing_token_is_401_with_a_bearer_challenge(protected_client: TestClient) -> None:
    response = protected_client.get("/whoami")
    assert response.status_code == 401
    assert response.json()["code"] == "NOT_AUTHENTICATED"
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "header", ["Token abc", "Bearer", "Bearer ", "Basic dXNlcjpwYXNz", "bearer"]
)
def test_a_malformed_authorization_header_is_401_not_authenticated(
    protected_client: TestClient, header: str
) -> None:
    response = protected_client.get("/whoami", headers={"Authorization": header})
    assert response.status_code == 401
    assert response.json()["code"] == "NOT_AUTHENTICATED"


# ---------------------------------------------------------------- 401 invalid token (AB14)


def test_alg_none_is_401_invalid_token(
    protected_client: TestClient, test_settings: Settings
) -> None:
    claims = {"sub": str(uuid.uuid4()), "exp": datetime.now(UTC) + timedelta(minutes=5)}
    token = jwt.encode(claims, None, algorithm="none")
    response = protected_client.get("/whoami", headers=bearer(token))
    assert response.status_code == 401
    assert response.json()["code"] == "INVALID_TOKEN"
    assert response.headers["www-authenticate"] == 'Bearer error="invalid_token"'


def test_a_token_signed_with_another_secret_is_401(protected_client: TestClient) -> None:
    other = jwt.encode(
        {"sub": str(uuid.uuid4()), "exp": datetime.now(UTC) + timedelta(minutes=5)},
        "z" * 40,
        algorithm="HS256",
    )
    response = protected_client.get("/whoami", headers=bearer(other))
    assert response.status_code == 401
    assert response.json()["code"] == "INVALID_TOKEN"


def test_an_expired_token_is_401(protected_client: TestClient, test_settings: Settings) -> None:
    past = datetime.now(UTC) - timedelta(hours=1)
    token = signed(test_settings, iat=past - timedelta(minutes=30), exp=past)
    response = protected_client.get("/whoami", headers=bearer(token))
    assert response.status_code == 401
    assert response.json()["code"] == "INVALID_TOKEN"


def test_garbage_is_401(protected_client: TestClient) -> None:
    response = protected_client.get("/whoami", headers=bearer("not.a.token"))
    assert response.status_code == 401
    assert response.json()["code"] == "INVALID_TOKEN"


def test_a_valid_token_for_an_unknown_user_is_401(
    protected_client: TestClient, test_settings: Settings
) -> None:
    response = protected_client.get("/whoami", headers=bearer(signed(test_settings)))
    assert response.status_code == 401
    assert response.json()["code"] == "INVALID_TOKEN"


def test_the_token_of_a_deleted_user_stops_working(
    protected_client: TestClient, test_settings: Settings, db_session: Session
) -> None:
    user = make_user(db_session)
    token = token_for(user, test_settings)
    assert protected_client.get("/whoami", headers=bearer(token)).status_code == 200
    db_session.execute(delete(User).where(User.id == user.id))
    db_session.commit()
    assert protected_client.get("/whoami", headers=bearer(token)).status_code == 401


# ---------------------------------------------------------------- the user and the role


def test_a_valid_token_gives_the_user(
    protected_client: TestClient, test_settings: Settings, db_session: Session
) -> None:
    user = make_user(db_session)
    response = protected_client.get("/whoami", headers=bearer(token_for(user, test_settings)))
    assert response.status_code == 200
    assert response.json() == {"id": str(user.id), "role": "PATIENT"}


def test_the_user_id_reaches_the_access_event(
    protected_client: TestClient, test_settings: Settings, db_session: Session
) -> None:
    user = make_user(db_session)
    with capture_logs() as logs:
        protected_client.get("/whoami", headers=bearer(token_for(user, test_settings)))
    (event,) = [entry for entry in logs if entry["event"] == "http.request"]
    assert event["user_id"] == str(user.id)


def test_the_lookup_is_one_primary_key_read(
    protected_client: TestClient, test_settings: Settings, db_session: Session
) -> None:
    user = make_user(db_session)
    engine = cast(FastAPI, protected_client.app).state.engine
    with QueryCounter(engine) as counter:
        protected_client.get("/whoami", headers=bearer(token_for(user, test_settings)))
    assert counter.count == 1
    assert AUTH_LOOKUP in counter.statements[0]


def test_a_patient_gets_403_on_an_admin_route_and_an_admin_gets_200(
    protected_client: TestClient, test_settings: Settings, db_session: Session
) -> None:
    patient = make_user(db_session)
    admin = make_user(db_session, role=Role.ADMIN)
    refused = protected_client.get("/admin-only", headers=bearer(token_for(patient, test_settings)))
    allowed = protected_client.get("/admin-only", headers=bearer(token_for(admin, test_settings)))
    assert refused.status_code == 403
    assert refused.json()["code"] == "FORBIDDEN"
    assert allowed.status_code == 200


def test_an_admin_route_without_a_token_is_401_not_403(protected_client: TestClient) -> None:
    assert protected_client.get("/admin-only").status_code == 401


def test_a_role_change_takes_effect_at_once(
    protected_client: TestClient, test_settings: Settings, db_session: Session
) -> None:
    """The token holds no role, so promoting the user needs no new token."""
    user = make_user(db_session)
    headers = bearer(token_for(user, test_settings))
    assert protected_client.get("/admin-only", headers=headers).status_code == 403
    user.role = Role.ADMIN
    db_session.commit()
    assert protected_client.get("/admin-only", headers=headers).status_code == 200

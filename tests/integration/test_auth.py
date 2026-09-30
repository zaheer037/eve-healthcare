"""Signup, login, token and the current user, over HTTP against a real database.

Covers AB1, AB6, AB16, AB19, AB21, AB22, AB25 and AB35 of the architecture document (§7.4).
"""

import uuid
from collections.abc import Callable
from typing import Any

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import select
from sqlalchemy.orm import Session
from structlog.contextvars import merge_contextvars
from structlog.testing import capture_logs
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.auth import service as auth_service
from app.core.config import Settings
from app.core.security import hash_password, verify_password
from app.core.throttle import account_key
from app.main import create_app
from app.models.user import Role, User
from tests.factories import make_user
from tests.support import AUTH_LOOKUP, QueryCounter, run_concurrently

PASSWORD = "correct horse battery staple"
PASSWORD_HASH = hash_password(PASSWORD)  # one hash for the whole module: 60 ms, once
ClientFactory = Callable[..., TestClient]


def new_email() -> str:
    return f"user-{uuid.uuid4().hex[:10]}@example.in"


def post_signup(client: TestClient, **overrides: Any) -> Response:
    return client.post(
        "/auth/signup", json={"email": new_email(), "password": PASSWORD, **overrides}
    )


def post_login(client: TestClient, email: str, password: str = PASSWORD) -> Response:
    return client.post("/auth/login", json={"email": email, "password": password})


def post_token(client: TestClient, username: str, password: str = PASSWORD) -> Response:
    """What Swagger's Authorize button sends."""
    form = {"grant_type": "password", "username": username, "password": password, "scope": ""}
    return client.post("/auth/token", data=form)


def problem_without_the_request_id(response: Response) -> dict[str, Any]:
    return {key: value for key, value in response.json().items() if key != "request_id"}


# ==== signup


def test_signup_creates_a_patient(client: TestClient, db_session: Session) -> None:
    response = post_signup(
        client, email="Jane.Doe@Example.IN", full_name=" Jane Doe ", phone="+919876543210"
    )
    body = response.json()
    assert response.status_code == 201
    assert set(body) == {"id", "email", "full_name", "phone", "role", "created_at"}
    assert body["email"] == "jane.doe@example.in"
    assert body["full_name"] == "Jane Doe"
    assert body["role"] == "PATIENT"
    assert "location" not in response.headers  # no route reads a user by ID
    user = db_session.scalar(select(User).where(User.id == uuid.UUID(body["id"])))
    assert user is not None
    assert user.password_hash.startswith("$argon2id$")
    assert PASSWORD not in user.password_hash


def test_the_signup_response_never_holds_the_hash_or_the_password(client: TestClient) -> None:
    text = post_signup(client).text
    assert "argon2" not in text
    assert PASSWORD not in text
    assert "password" not in text


def test_a_second_signup_with_the_same_email_is_409(client: TestClient) -> None:
    email = new_email()
    assert post_signup(client, email=email).status_code == 201
    response = post_signup(client, email=email)
    assert response.status_code == 409
    assert response.json()["code"] == "EMAIL_ALREADY_REGISTERED"


def test_an_email_in_another_case_is_the_same_account(client: TestClient) -> None:
    email = new_email()
    assert post_signup(client, email=email).status_code == 201
    assert post_signup(client, email=email.upper()).status_code == 409


def test_a_14_character_password_is_a_422_that_names_the_field(client: TestClient) -> None:
    response = post_signup(client, password="x" * 14)
    body = response.json()
    assert response.status_code == 422
    assert [item["field"] for item in body["errors"]] == ["body.password"]
    assert "x" * 14 not in response.text  # the rejected input is never echoed


def test_a_15_character_password_is_accepted(client: TestClient) -> None:
    assert post_signup(client, password="x" * 15).status_code == 201


def test_the_minimum_length_follows_the_setting(client_with: ClientFactory) -> None:
    lenient = client_with(password_min_length=8)
    assert post_signup(lenient, password="x" * 8).status_code == 201
    assert post_signup(lenient, password="x" * 7).status_code == 422


def test_a_password_with_leading_and_trailing_spaces_keeps_them(client: TestClient) -> None:
    padded = "  a padded pass phrase  "
    email = new_email()
    assert post_signup(client, email=email, password=padded).status_code == 201
    assert post_login(client, email, padded).status_code == 200
    assert post_login(client, email, padded.strip()).status_code == 401


def test_a_role_field_is_a_422_and_creates_no_admin(
    client: TestClient, db_session: Session
) -> None:
    """AB6."""
    email = new_email()
    response = post_signup(client, email=email, role="ADMIN")
    assert response.status_code == 422
    assert db_session.scalar(select(User).where(User.email == email)) is None


@pytest.mark.parametrize("control", ["\x00", "\t", "\n", "\N{RIGHT-TO-LEFT OVERRIDE}"])
def test_control_characters_in_a_name_or_a_password_are_a_422(
    client: TestClient, control: str
) -> None:
    """AB25."""
    assert post_signup(client, full_name=f"Jane{control}Doe").status_code == 422
    assert post_signup(client, password=f"a long enough {control} password").status_code == 422


def test_a_200_character_password_is_a_422(client: TestClient) -> None:
    assert post_signup(client, password="p" * 200).status_code == 422


def test_an_oversized_email_is_a_422_and_a_huge_body_is_a_413(client: TestClient) -> None:
    """AB25 and AB16: the body cap answers first for a body over 64 KiB."""
    assert (
        client.post("/auth/login", json={"email": "a" * 10_000, "password": "x"}).status_code == 422
    )
    huge = client.post("/auth/login", json={"email": "a" * 100_000, "password": "x"})
    assert huge.status_code == 413
    assert huge.json()["code"] == "PAYLOAD_TOO_LARGE"


def test_a_missing_field_is_a_422(client: TestClient) -> None:
    response = client.post("/auth/signup", json={"email": new_email()})
    assert response.status_code == 422
    assert [item["field"] for item in response.json()["errors"]] == ["body.password"]


def test_signup_runs_one_statement(
    app: FastAPI, client: TestClient, query_counter: QueryCounter
) -> None:
    """The budget of the design: one INSERT ... RETURNING and no pre-check SELECT."""
    with query_counter:
        assert post_signup(client).status_code == 201
    assert query_counter.count == 1
    assert query_counter.statements[0].startswith("INSERT INTO users")


@pytest.mark.concurrency
def test_two_simultaneous_signups_with_one_email_give_one_201_and_one_409(
    app: FastAPI, client: TestClient
) -> None:
    email = new_email()
    calls = [lambda: post_signup(TestClient(app), email=email).status_code for _ in range(2)]
    assert sorted(run_concurrently(calls)) == [201, 409]


# ==== login


@pytest.mark.parametrize("path", ["/auth/login", "/auth/token"])
def test_a_patient_logs_in_on_both_endpoints(
    client: TestClient, db_session: Session, test_settings: Settings, path: str
) -> None:
    user = make_user(db_session, password_hash=PASSWORD_HASH)
    response = (
        post_login(client, user.email) if path == "/auth/login" else post_token(client, user.email)
    )
    body = response.json()
    assert response.status_code == 200
    assert set(body) == {"access_token", "token_type", "expires_in"}
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 1800
    assert response.headers["cache-control"] == "no-store"  # RFC 6749 section 5.1
    assert response.headers["pragma"] == "no-cache"
    claims = jwt.decode(body["access_token"], options={"verify_signature": False})
    assert set(claims) == {"sub", "iat", "exp", "iss", "aud"}
    assert claims["sub"] == str(user.id)


@pytest.mark.parametrize("path", ["/auth/login", "/auth/token"])
def test_an_admin_that_the_test_creates_logs_in_on_both_endpoints(
    client: TestClient, db_session: Session, path: str
) -> None:
    admin = make_user(db_session, password_hash=PASSWORD_HASH, role=Role.ADMIN)
    response = (
        post_login(client, admin.email)
        if path == "/auth/login"
        else post_token(client, admin.email)
    )
    assert response.status_code == 200
    token = response.json()["access_token"]
    me = client.get("/users/me", headers={"Authorization": f"Bearer {token}"})
    assert me.json()["role"] == "ADMIN"


def test_a_login_email_in_another_case_still_works(client: TestClient, db_session: Session) -> None:
    user = make_user(db_session, password_hash=PASSWORD_HASH)
    assert post_login(client, user.email.upper()).status_code == 200


def test_a_wrong_password_is_401_invalid_credentials(
    client: TestClient, db_session: Session
) -> None:
    user = make_user(db_session, password_hash=PASSWORD_HASH)
    response = post_login(client, user.email, "wrong pass phrase")
    assert response.status_code == 401
    assert response.json()["code"] == "INVALID_CREDENTIALS"
    assert response.headers["www-authenticate"] == "Bearer"


def test_an_unknown_email_and_a_wrong_password_give_the_same_401(
    client: TestClient, db_session: Session
) -> None:
    """AB19: the two bodies are identical, so a probe cannot tell which emails exist."""
    user = make_user(db_session, password_hash=PASSWORD_HASH)
    wrong_password = post_login(client, user.email, "wrong pass phrase")
    unknown_email = post_login(client, new_email())
    assert wrong_password.status_code == unknown_email.status_code == 401
    assert problem_without_the_request_id(wrong_password) == problem_without_the_request_id(
        unknown_email
    )
    assert dict(wrong_password.headers).keys() >= {"www-authenticate"}
    assert wrong_password.headers["www-authenticate"] == unknown_email.headers["www-authenticate"]


def test_an_unknown_email_is_verified_against_the_dummy_hash(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dummy hash runs, so an unknown email takes as long as a wrong password."""
    hashes_verified: list[str] = []

    def spy(plain: str, hashed: str) -> bool:
        hashes_verified.append(hashed)
        return verify_password(plain, hashed)

    monkeypatch.setattr(auth_service, "verify_password", spy)
    assert post_login(client, new_email()).status_code == 401
    assert hashes_verified == [app.state.dummy_hash]


def test_a_login_email_that_no_signup_would_accept_can_still_log_in(
    client: TestClient, db_session: Session
) -> None:
    """The seeded admin may sit at a reserved domain that EmailStr refuses."""
    admin = make_user(
        db_session, email="admin@eve.local", password_hash=PASSWORD_HASH, role=Role.ADMIN
    )
    assert post_login(client, admin.email).status_code == 200
    assert post_signup(client, email="other@eve.local").status_code == 422


def test_login_runs_one_statement(app: FastAPI, client: TestClient, db_session: Session) -> None:
    user = make_user(db_session, password_hash=PASSWORD_HASH)
    with QueryCounter(app.state.engine) as counter:
        assert post_login(client, user.email).status_code == 200
    assert counter.count == 1
    assert "WHERE users.email" in counter.statements[0]


def test_login_refuses_extra_fields(client: TestClient) -> None:
    response = client.post(
        "/auth/login", json={"email": new_email(), "password": "x", "remember_me": True}
    )
    assert response.status_code == 422


@pytest.mark.parametrize("path", ["/auth/login", "/auth/signup"])
def test_a_lone_surrogate_in_a_password_is_422_and_not_a_500(client: TestClient, path: str) -> None:
    """Python's JSON reader accepts `\\ud800`, and no UTF-8 encoder does (AB25)."""
    body = b'{"email":"a@example.in","password":"long enough \\ud800 password"}'
    response = client.post(path, content=body, headers={"content-type": "application/json"})
    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_ERROR")
    assert [error["field"] for error in response.json()["errors"]] == ["body.password"]


# ==== throttle (AB21, AB22, AB35)


def test_the_eleventh_bad_login_for_one_account_is_429_with_retry_after(
    client: TestClient, db_session: Session
) -> None:
    """AB21, with the default limit of ten failures in 15 minutes."""
    user = make_user(db_session, password_hash=PASSWORD_HASH)
    for _ in range(10):
        assert post_login(client, user.email, "wrong pass phrase").status_code == 401
    refused = post_login(client, user.email, PASSWORD)  # even the right password is refused now
    assert refused.status_code == 429
    assert refused.json()["code"] == "RATE_LIMITED"
    assert 1 <= int(refused.headers["retry-after"]) <= 15 * 60


def test_a_failed_login_logs_the_client_and_a_short_account_key_and_never_the_email(
    client: TestClient, logs: list[dict[str, Any]], db_session: Session
) -> None:
    user = make_user(db_session, password_hash=PASSWORD_HASH)
    assert post_login(client, user.email, "wrong pass phrase").status_code == 401
    (failed,) = [line for line in logs if line["event"] == "auth.login_failed"]
    assert failed["log_level"] == "warning"
    assert failed["client"] == "testclient"  # the gate binds it, as the request ID is bound
    assert failed["account"] == account_key(user.email)[:12]
    assert user.email not in str(logs)


def test_a_throttled_login_is_logged_with_its_scope_and_a_retry_hint(
    client_with: ClientFactory,
) -> None:
    client = client_with(login_max_failures=2)
    email = new_email()
    with capture_logs(processors=[merge_contextvars]) as logs:
        for _ in range(2):
            post_login(client, email)
        assert post_login(client, email).status_code == 429
    (throttled,) = [line for line in logs if line["event"] == "auth.throttled"]
    assert (throttled["log_level"], throttled["scope"]) == ("warning", "account")
    assert (throttled["client"], throttled["retry_after"] >= 1) == ("testclient", True)
    assert email not in str(logs)


def test_the_account_limit_also_covers_an_email_that_does_not_exist(
    client_with: ClientFactory,
) -> None:
    client = client_with(login_max_failures=3)
    email = new_email()
    for _ in range(3):
        assert post_login(client, email).status_code == 401
    assert post_login(client, email).status_code == 429


def test_case_variants_of_one_email_share_the_failure_counter(client_with: ClientFactory) -> None:
    client = client_with(login_max_failures=3)
    email = new_email()
    for spelling in (email, email.upper(), email.title()):
        assert post_login(client, spelling).status_code == 401
    assert post_login(client, email.swapcase()).status_code == 429


def test_a_good_login_clears_the_failure_counter(
    client_with: ClientFactory, db_session: Session
) -> None:
    client = client_with(login_max_failures=3)
    user = make_user(db_session, password_hash=PASSWORD_HASH)
    for _ in range(2):
        post_login(client, user.email, "wrong pass phrase")
    assert post_login(client, user.email).status_code == 200  # clears the two failures
    for _ in range(2):
        assert post_login(client, user.email, "wrong pass phrase").status_code == 401
    assert post_login(client, user.email).status_code == 200


def test_the_token_endpoint_shares_the_account_counter_with_login(
    client_with: ClientFactory,
) -> None:
    client = client_with(login_max_failures=2)
    email = new_email()
    assert post_login(client, email).status_code == 401
    assert post_token(client, email).status_code == 401
    assert post_token(client, email).status_code == 429


def test_more_than_the_ip_limit_of_auth_requests_in_a_minute_is_429(
    client_with: ClientFactory,
) -> None:
    """AB22: the limit counts signup, login and token together."""
    client = client_with(auth_ip_limit_per_minute=3)
    assert post_login(client, new_email()).status_code == 401
    assert post_token(client, new_email()).status_code == 401
    assert post_signup(client, password="x" * 15).status_code == 201
    refused = post_login(client, new_email())
    assert refused.status_code == 429
    assert int(refused.headers["retry-after"]) >= 1


def test_a_request_that_fails_validation_does_not_count_against_the_ip_limit(
    client_with: ClientFactory,
) -> None:
    client = client_with(auth_ip_limit_per_minute=2)
    for _ in range(5):
        assert client.post("/auth/login", json={"email": ""}).status_code == 422
    assert post_login(client, new_email()).status_code == 401


def test_a_spoofed_forwarded_for_header_does_not_change_the_throttle_key(
    test_settings: Settings,
) -> None:
    """AB35: behind a proxy, the key is the address that the proxy appended."""
    settings = test_settings.model_copy(update={"auth_ip_limit_per_minute": 3})
    proxied = ProxyHeadersMiddleware(create_app(settings), trusted_hosts=["203.0.113.9"])
    real_address = "198.51.100.7"
    with TestClient(
        proxied, client=("203.0.113.9", 50000)
    ) as through_the_proxy:  # the proxy itself
        for spoof in ("6.6.6.6", "7.7.7.7", "8.8.8.8"):
            headers = {"X-Forwarded-For": f"{spoof}, {real_address}"}
            body = {"email": new_email(), "password": "x"}
            assert (
                through_the_proxy.post("/auth/login", json=body, headers=headers).status_code == 401
            )
        headers = {"X-Forwarded-For": f"9.9.9.9, {real_address}"}
        blocked = through_the_proxy.post(
            "/auth/login", json={"email": new_email(), "password": "x"}, headers=headers
        )
        assert blocked.status_code == 429  # another spoofed prefix, the same real address
        other = through_the_proxy.post(
            "/auth/login",
            json={"email": new_email(), "password": "x"},
            headers={"X-Forwarded-For": "6.6.6.6, 198.51.100.8"},
        )
        assert other.status_code == 401  # another real address is not limited


def test_the_addresses_of_one_ipv6_64_behind_the_proxy_share_one_budget(
    test_settings: Settings,
) -> None:
    """AB38: a client that owns a /64 cannot get a fresh budget by changing its address."""
    settings = test_settings.model_copy(update={"auth_ip_limit_per_minute": 3})
    proxied = ProxyHeadersMiddleware(create_app(settings), trusted_hosts=["203.0.113.9"])
    with TestClient(proxied, client=("203.0.113.9", 50000)) as through_the_proxy:
        for number in range(3):
            headers = {"X-Forwarded-For": f"2001:db8:abcd:12::{number:x}"}
            body = {"email": new_email(), "password": "x"}
            assert (
                through_the_proxy.post("/auth/login", json=body, headers=headers).status_code == 401
            )
        fresh = {"X-Forwarded-For": "2001:db8:abcd:12:aaaa:bbbb:cccc:dddd"}
        body = {"email": new_email(), "password": "x"}
        assert through_the_proxy.post("/auth/login", json=body, headers=fresh).status_code == 429
        next_network = {"X-Forwarded-For": "2001:db8:abcd:13::1"}
        assert (
            through_the_proxy.post("/auth/login", json=body, headers=next_network).status_code
            == 401
        )


def test_every_hashing_slot_taken_is_a_503_at_once(client_with: ClientFactory) -> None:
    client = client_with(hash_max_concurrency=1)
    slots = client.app.state.hash_slots  # type: ignore[attr-defined]
    with slots.slot():
        for response in (
            post_login(client, new_email()),
            post_token(client, new_email()),
            post_signup(client),
        ):
            assert response.status_code == 503
            assert response.json()["code"] == "SERVICE_UNAVAILABLE"
            assert response.headers["retry-after"] == "1"
    assert post_login(client, new_email()).status_code == 401  # the slot is free again


# ==== users/me


def test_me_returns_the_signed_in_user(client: TestClient, db_session: Session) -> None:
    user = make_user(db_session, password_hash=PASSWORD_HASH, full_name="Jane Doe")
    token = post_login(client, user.email).json()["access_token"]
    response = client.get("/users/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json()["id"] == str(user.id)
    assert response.json()["full_name"] == "Jane Doe"
    assert "password" not in response.text
    assert "argon2" not in response.text


def test_me_without_a_token_is_401(client: TestClient) -> None:
    """AB1, on the one protected route that exists so far."""
    response = client.get("/users/me")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_me_runs_no_statement_beyond_the_authentication_lookup(
    app: FastAPI, client: TestClient, db_session: Session
) -> None:
    user = make_user(db_session, password_hash=PASSWORD_HASH)
    token = post_login(client, user.email).json()["access_token"]
    with QueryCounter(app.state.engine, ignore=(AUTH_LOOKUP,)) as counter:
        assert (
            client.get("/users/me", headers={"Authorization": f"Bearer {token}"}).status_code == 200
        )
    assert counter.count == 0


# ==== documentation


def test_swagger_can_log_in_with_the_form_endpoint(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()
    scheme = spec["components"]["securitySchemes"]["OAuth2PasswordBearer"]
    assert scheme["flows"]["password"]["tokenUrl"] == "/auth/token"
    token_body = spec["paths"]["/auth/token"]["post"]["requestBody"]["content"]
    assert "application/x-www-form-urlencoded" in token_body
    login_body = spec["paths"]["/auth/login"]["post"]["requestBody"]["content"]
    assert "application/json" in login_body

"""The webhook's raw-body dependency: signature first, then the body (architecture §§2.4, 6.5)."""

import time
import uuid
from typing import Annotated, Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response

from app.core.config import Settings
from app.core.problem import install_exception_handlers
from app.payments.dependencies import verified_webhook_payload
from app.payments.schemas import WebhookPayload
from app.payments.signature import header_for
from tests.support import SettingsFactory

PAYMENT_ID = str(uuid.uuid4())
SIGNATURE = "X-Webhook-Signature"


def build_client(settings: Settings) -> TestClient:
    app = FastAPI()
    app.state.settings = settings
    install_exception_handlers(app)

    @app.post("/hook/")
    def hook(
        payload: Annotated[WebhookPayload, Depends(verified_webhook_payload)],
    ) -> dict[str, str]:
        return {"event_id": payload.event_id, "status": payload.status.value}

    return TestClient(app)


@pytest.fixture
def secret(settings: Settings) -> str:
    return settings.webhook_secret.get_secret_value()


@pytest.fixture
def client(settings: Settings) -> TestClient:
    return build_client(settings)


def body_of(**changes: Any) -> bytes:
    fields = {"event_id": "evt_1", "payment_id": PAYMENT_ID, "status": "SUCCESS", **changes}
    items = ",".join(
        f'"{key}":{value!r}'.replace("'", '"') if isinstance(value, str) else f'"{key}":{value}'
        for key, value in fields.items()
    )
    return ("{" + items + "}").encode()


def post(client: TestClient, body: bytes, header: str | None) -> Response:
    headers = {"Content-Type": "application/json"}
    if header is not None:
        headers[SIGNATURE] = header
    return client.post("/hook/", content=body, headers=headers)


def signed(secret: str, body: bytes, *, age: int = 0) -> str:
    return header_for(secret, int(time.time()) - age, body)


# ==== the signature comes first


def test_a_signed_valid_body_reaches_the_handler(client: TestClient, secret: str) -> None:
    body = body_of()
    response = post(client, body, signed(secret, body))
    assert response.status_code == 200
    assert response.json() == {"event_id": "evt_1", "status": "SUCCESS"}


@pytest.mark.parametrize("kind", ["missing", "wrong-secret", "changed-body", "stale", "malformed"])
def test_a_bad_signature_is_401_with_one_code_and_a_signature_challenge(
    client: TestClient, secret: str, kind: str
) -> None:
    body = body_of()
    header = {
        "missing": None,
        "wrong-secret": signed("another-secret-of-the-same-sort-0123456789", body),
        "changed-body": signed(secret, body_of(event_id="evt_2")),
        "stale": signed(secret, body, age=400),
        "malformed": "not a signature",
    }[kind]
    response = post(client, body, header)
    assert response.status_code == 401
    assert response.json()["code"] == "WEBHOOK_SIGNATURE_INVALID"
    assert response.headers["www-authenticate"] == 'Signature realm="webhook"'


def test_a_bad_signature_is_401_even_when_the_json_is_invalid(client: TestClient) -> None:
    """The handler declares no body, so FastAPI never parses it. The signature comes first."""
    response = post(client, b"this is not json", "t=1,v1=" + "0" * 64)
    assert response.status_code == 401


# ==== then the body


@pytest.mark.parametrize(
    ("body", "field"),
    [
        (body_of(status="REFUNDED"), "body.status"),
        (b'{"payment_id":"%s","status":"SUCCESS"}' % PAYMENT_ID.encode(), "body.event_id"),
        (body_of(payment_id="nope"), "body.payment_id"),
        (
            b'{"event_id":"a\\u0000b","payment_id":"%s","status":"SUCCESS"}' % PAYMENT_ID.encode(),
            "body.event_id",
        ),
        (body_of(amount="-5"), "body.amount"),
    ],
)
def test_a_signed_body_with_a_bad_field_is_422_and_names_the_field_only(
    client: TestClient, secret: str, body: bytes, field: str
) -> None:
    response = post(client, body, signed(secret, body))
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert [item["field"] for item in response.json()["errors"]] == [field]
    assert "REFUNDED" not in response.text  # the rejected value is never echoed


@pytest.mark.parametrize("body", [b"", b"[]", b"not json", b"\xff\xfe\x00", b"null"])
def test_a_signed_body_that_is_not_an_event_is_422(
    client: TestClient, secret: str, body: bytes
) -> None:
    assert post(client, body, signed(secret, body)).status_code == 422


# ==== the setting


def test_the_check_can_be_switched_off_outside_production(make_settings: SettingsFactory) -> None:
    client = build_client(make_settings(environment="test", webhook_signature_required=False))
    assert post(client, body_of(), None).status_code == 200
    assert post(client, body_of(status="REFUNDED"), None).status_code == 422  # still validated


def test_a_wider_tolerance_is_a_setting(make_settings: SettingsFactory) -> None:
    settings = make_settings(environment="test", webhook_tolerance_seconds=3600)
    body = body_of()
    header = signed(settings.webhook_secret.get_secret_value(), body, age=1800)
    assert post(build_client(settings), body, header).status_code == 200

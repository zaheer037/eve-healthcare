"""Payment and webhook models (architecture §§5.6 and 6.5)."""

import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from app.models.booking import BookingStatus
from app.models.payment import PaymentStatus, WebhookOutcome
from app.payments.schemas import (
    BookingRef,
    PaymentCreate,
    PaymentRead,
    WebhookAck,
    WebhookPayload,
)

PAYMENT_ID = str(uuid.uuid4())
DOCUMENTED_BODY = (
    '{"event_id":"evt_01J8ZQ4M3K7W","payment_id":"0d4b7a52-3f1e-4d0a-9b8c-2f6a1c7e9d10",'
    '"status":"SUCCESS","provider_payment_id":"sim_0d4b7a523f1e4d0a9b8c2f6a1c7e9d10",'
    '"amount":"499.00","currency":"INR"}'
)


def event(**changes: Any) -> dict[str, Any]:
    return {"event_id": "evt_1", "payment_id": PAYMENT_ID, "status": "SUCCESS", **changes}


def refused_fields(**changes: Any) -> set[str]:
    with pytest.raises(ValidationError) as error:
        WebhookPayload.model_validate(event(**changes))
    return {str(item["loc"][0]) for item in error.value.errors()}


# ==== WebhookPayload


def test_the_documented_body_parses() -> None:
    payload = WebhookPayload.model_validate_json(DOCUMENTED_BODY)
    assert payload.event_id == "evt_01J8ZQ4M3K7W"
    assert payload.status is PaymentStatus.SUCCESS
    assert payload.amount == Decimal("499.00")
    assert payload.currency == "INR"
    assert payload.provider_payment_id == "sim_0d4b7a523f1e4d0a9b8c2f6a1c7e9d10"


def test_only_three_fields_are_required() -> None:
    payload = WebhookPayload.model_validate(event())
    assert (payload.amount, payload.currency, payload.provider_payment_id) == (None, None, None)
    assert payload.failure_reason is None


def test_unknown_fields_are_ignored_and_never_stored() -> None:
    """A provider may add fields. It may also name a booking, which the receiver never trusts."""
    payload = WebhookPayload.model_validate(
        event(booking_id=str(uuid.uuid4()), new_field={"a": 1}, amount="10.00")
    )
    assert set(payload.model_dump(mode="json")) == {
        "event_id",
        "payment_id",
        "status",
        "provider_payment_id",
        "amount",
        "currency",
        "failure_reason",
    }


def test_the_stored_payload_is_plain_json() -> None:
    stored = WebhookPayload.model_validate_json(DOCUMENTED_BODY).model_dump(mode="json")
    assert json.loads(json.dumps(stored)) == stored
    assert stored["amount"] == "499.00"
    assert stored["status"] == "SUCCESS"
    assert stored["payment_id"] == "0d4b7a52-3f1e-4d0a-9b8c-2f6a1c7e9d10"


@pytest.mark.parametrize("status", ["PENDING", "REFUNDED", "success", "", None, 1, "SUCCESS "])
def test_the_status_is_success_or_failed_exactly(status: Any) -> None:
    assert refused_fields(status=status) == {"status"}


def test_a_failed_status_parses() -> None:
    payload = WebhookPayload.model_validate(event(status="FAILED", failure_reason="card_declined"))
    assert payload.status is PaymentStatus.FAILED
    assert payload.failure_reason == "card_declined"


def test_a_success_event_carries_no_failure_reason() -> None:
    """A payment that succeeded has nothing to explain, so a reason on it is a broken contract."""
    assert refused_fields(status="SUCCESS", failure_reason="card_declined") == {"failure_reason"}
    assert WebhookPayload.model_validate(event(status="SUCCESS", failure_reason=None))


def test_a_bad_status_is_the_only_error_when_a_reason_comes_with_it() -> None:
    """The reason is judged against a status that was valid. Otherwise one mistake shows twice."""
    assert refused_fields(status="REFUNDED", failure_reason="card_declined") == {"status"}


@pytest.mark.parametrize(
    "event_id",
    ["", "   ", "x" * 129, "evt\x00", "evt\n1", "evt\N{RIGHT-TO-LEFT OVERRIDE}1", None, 7],
)
def test_the_event_id_is_1_to_128_characters_without_control_characters(event_id: Any) -> None:
    assert refused_fields(event_id=event_id) == {"event_id"}


def test_the_event_id_at_its_limits_is_accepted() -> None:
    assert WebhookPayload.model_validate(event(event_id="x")).event_id == "x"
    assert len(WebhookPayload.model_validate(event(event_id="x" * 128)).event_id) == 128


@pytest.mark.parametrize("payment_id", ["not-a-uuid", "", None, 7])
def test_the_payment_id_must_be_a_uuid(payment_id: Any) -> None:
    assert refused_fields(payment_id=payment_id) == {"payment_id"}


@pytest.mark.parametrize("field", ["event_id", "payment_id", "status"])
def test_the_three_required_fields_cannot_be_missing(field: str) -> None:
    body = event()
    del body[field]
    with pytest.raises(ValidationError) as error:
        WebhookPayload.model_validate(body)
    assert [item["loc"] for item in error.value.errors()] == [(field,)]


@pytest.mark.parametrize(
    "amount", ["0", "0.00", "-1", "100000000", "1.005", "NaN", "Infinity", "abc", "", [], {}]
)
def test_an_amount_outside_the_money_rules_is_refused(amount: Any) -> None:
    assert refused_fields(amount=amount) == {"amount"}


@pytest.mark.parametrize("amount", ["0.01", "99999999.99", "499", 499.5, 10])
def test_an_amount_inside_the_money_rules_is_accepted(amount: Any) -> None:
    assert WebhookPayload.model_validate(event(amount=amount)).amount is not None


@pytest.mark.parametrize("currency", ["inr", "INRR", "IN", "", "₹₹₹"])
def test_a_currency_is_three_upper_case_letters(currency: str) -> None:
    assert refused_fields(currency=currency) == {"currency"}


@pytest.mark.parametrize("reference", ["", "x" * 129, "sim\x00", "sim\t1"])
def test_a_provider_reference_is_1_to_128_characters_without_control_characters(
    reference: str,
) -> None:
    assert refused_fields(provider_payment_id=reference) == {"provider_payment_id"}


@pytest.mark.parametrize("reason", ["", "x" * 201, "declined\x00", "a\nb"])
def test_a_failure_reason_has_no_control_characters(reason: str) -> None:
    assert refused_fields(failure_reason=reason) == {"failure_reason"}


@pytest.mark.parametrize(
    "raw",
    [b"", b"not json", b"[]", b"null", b'"text"', b"\xff\xfe", b'{"event_id":', b"{}"],
)
def test_a_body_that_is_not_an_event_object_fails_validation(raw: bytes) -> None:
    with pytest.raises(ValidationError):
        WebhookPayload.model_validate_json(raw)


def test_a_nul_escape_in_json_is_refused_so_the_jsonb_insert_cannot_fail() -> None:
    with pytest.raises(ValidationError):
        WebhookPayload.model_validate_json(
            b'{"event_id":"a\\u0000b","payment_id":"%s","status":"SUCCESS"}' % PAYMENT_ID.encode()
        )


# ==== WebhookAck


def test_an_acknowledgement_for_a_processed_event_names_the_outcome() -> None:
    ack = WebhookAck(status="processed", outcome=WebhookOutcome.APPLIED)
    assert ack.model_dump(mode="json") == {
        "status": "processed",
        "outcome": "APPLIED",
        "detail": None,
    }


def test_an_acknowledgement_for_a_duplicate_has_no_outcome() -> None:
    assert WebhookAck(status="duplicate").model_dump(mode="json") == {
        "status": "duplicate",
        "outcome": None,
        "detail": None,
    }


def test_an_acknowledgement_has_two_statuses_only() -> None:
    with pytest.raises(ValidationError):
        WebhookAck(status="ok")  # type: ignore[arg-type]


# ==== PaymentCreate


def test_a_payment_request_needs_only_the_booking() -> None:
    payment = PaymentCreate.model_validate({"booking_id": PAYMENT_ID})
    assert payment.simulate is None


@pytest.mark.parametrize("simulate", ["SUCCESS", "FAILED", "PENDING"])
def test_a_payment_request_may_simulate_a_result(simulate: str) -> None:
    assert PaymentCreate.model_validate({"booking_id": PAYMENT_ID, "simulate": simulate}).simulate


@pytest.mark.parametrize("simulate", ["success", "REFUNDED", "", 1, True])
def test_the_simulated_result_is_one_of_three_exactly(simulate: Any) -> None:
    with pytest.raises(ValidationError):
        PaymentCreate.model_validate({"booking_id": PAYMENT_ID, "simulate": simulate})


@pytest.mark.parametrize("field", ["amount", "status", "currency", "user_id", "provider"])
def test_a_client_never_sends_the_amount_or_any_other_server_field(field: str) -> None:
    """The amount always comes from the booking."""
    with pytest.raises(ValidationError) as error:
        PaymentCreate.model_validate({"booking_id": PAYMENT_ID, field: "1"})
    assert {str(item["loc"][0]) for item in error.value.errors()} == {field}


def test_the_booking_id_must_be_a_uuid_and_is_required() -> None:
    for body in ({}, {"booking_id": "x"}, {"booking_id": None}):
        with pytest.raises(ValidationError):
            PaymentCreate.model_validate(body)


# ==== PaymentRead


def test_a_payment_reads_with_money_as_a_string_and_its_booking_status() -> None:
    read = PaymentRead(
        id=uuid.uuid4(),
        booking_id=uuid.UUID(PAYMENT_ID),
        amount=Decimal("499.00"),
        currency="INR",
        status=PaymentStatus.SUCCESS,
        failure_reason=None,
        settled_at=datetime(2030, 1, 1, 12, 0, tzinfo=UTC),
        booking=BookingRef(id=uuid.UUID(PAYMENT_ID), status=BookingStatus.CONFIRMED),
    )
    body = read.model_dump(mode="json")
    assert body["amount"] == "499.00"
    assert body["settled_at"] == "2030-01-01T12:00:00Z"
    assert body["booking"] == {"id": PAYMENT_ID, "status": "CONFIRMED"}
    assert set(body) == {
        "id",
        "booking_id",
        "amount",
        "currency",
        "status",
        "failure_reason",
        "settled_at",
        "booking",
    }

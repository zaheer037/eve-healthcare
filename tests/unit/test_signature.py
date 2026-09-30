"""Webhook signatures (spec section 8.5): sign and verify agree, and every failure looks alike."""

from datetime import UTC, datetime, timedelta

import pytest

from app.core.errors import WebhookSignatureInvalid
from app.payments.signature import header_for, sign, verify

# The example of section 8.5. The signature in the spec was made with `openssl` as well.
SECRET = "whsec_demo_only_not_a_real_secret_0123456789"
TIMESTAMP = 1790000000
BODY = (
    b'{"event_id":"evt_01J8ZQ4M3K7W","payment_id":"0d4b7a52-3f1e-4d0a-9b8c-2f6a1c7e9d10",'
    b'"status":"SUCCESS","provider_payment_id":"sim_0d4b7a523f1e4d0a9b8c2f6a1c7e9d10",'
    b'"amount":"499.00","currency":"INR"}'
)
DOCUMENTED = "afce76fa350828580fc1b9c5ad8784b180b096922bb87e29b10d108a7b51b385"
NOW = datetime.fromtimestamp(TIMESTAMP, tz=UTC)
GOOD_HEADER = f"t={TIMESTAMP},v1={DOCUMENTED}"


def check(header: str | None, body: bytes = BODY, *, now: datetime = NOW) -> None:
    verify(SECRET, header, body, now=now)


# ==== sign


def test_the_documented_example_reproduces() -> None:
    assert sign(SECRET, TIMESTAMP, BODY) == DOCUMENTED
    assert header_for(SECRET, TIMESTAMP, BODY) == GOOD_HEADER


def test_the_signature_covers_the_timestamp_the_secret_and_every_byte_of_the_body() -> None:
    assert sign(SECRET, TIMESTAMP + 1, BODY) != DOCUMENTED
    assert sign(SECRET + "x", TIMESTAMP, BODY) != DOCUMENTED
    assert sign(SECRET, TIMESTAMP, BODY + b" ") != DOCUMENTED
    assert sign(SECRET, TIMESTAMP, BODY.replace(b"499.00", b"499.01")) != DOCUMENTED


def test_reformatted_json_breaks_the_signature() -> None:
    """The signature covers the raw bytes, so a proxy that pretty-prints the body breaks it."""
    reformatted = BODY.replace(b",", b", ")
    with pytest.raises(WebhookSignatureInvalid):
        check(GOOD_HEADER, reformatted)


# ==== verify: accept


def test_a_correct_signature_passes() -> None:
    check(GOOD_HEADER)


def test_the_hex_may_be_upper_case() -> None:
    check(f"t={TIMESTAMP},v1={DOCUMENTED.upper()}")


def test_a_header_may_carry_a_part_that_this_version_does_not_know() -> None:
    check(f"t={TIMESTAMP},v1={DOCUMENTED},v0=whatever")


def test_spaces_around_the_parts_are_tolerated() -> None:
    check(f"t={TIMESTAMP}, v1={DOCUMENTED}")


@pytest.mark.parametrize("skew", [-300, -1, 0, 1, 300])
def test_a_timestamp_within_five_minutes_passes_in_both_directions(skew: int) -> None:
    check(GOOD_HEADER, now=NOW + timedelta(seconds=skew))


# ==== verify: refuse, and always with the same error


@pytest.mark.parametrize("skew", [-301, 301, -3600, 86400])
def test_a_stale_or_future_timestamp_is_refused(skew: int) -> None:
    with pytest.raises(WebhookSignatureInvalid):
        check(GOOD_HEADER, now=NOW + timedelta(seconds=skew))


def test_the_tolerance_is_a_parameter() -> None:
    later = NOW + timedelta(seconds=30)
    verify(SECRET, GOOD_HEADER, BODY, now=later, tolerance_seconds=30)
    with pytest.raises(WebhookSignatureInvalid):
        verify(SECRET, GOOD_HEADER, BODY, now=later, tolerance_seconds=29)


WRONG_HEADERS = [
    None,
    "",
    "   ",
    "garbage",
    "t=,v1=",
    f"v1={DOCUMENTED}",  # no timestamp
    f"t={TIMESTAMP}",  # no signature
    f"t=abc,v1={DOCUMENTED}",
    f"t=-{TIMESTAMP},v1={DOCUMENTED}",
    f"t=+{TIMESTAMP},v1={DOCUMENTED}",
    f"t={TIMESTAMP}.5,v1={DOCUMENTED}",
    f"t={TIMESTAMP} ,v1={DOCUMENTED}x",
    "t=١٧٩٠٠٠٠٠٠٠,v1=" + DOCUMENTED,  # Arabic digits
    f"t={TIMESTAMP},v1={DOCUMENTED[:-1]}",  # one character short
    f"t={TIMESTAMP},v1={DOCUMENTED}00",  # too long
    f"t={TIMESTAMP},v1={'0' * 64}",  # well formed, wrong
    f"t={TIMESTAMP},v1={'z' * 64}",  # not hex
    f"t={TIMESTAMP},v1={DOCUMENTED},v1={DOCUMENTED}",  # a part twice
    f"t={TIMESTAMP},t={TIMESTAMP},v1={DOCUMENTED}",
    f"t={TIMESTAMP},v1=é{DOCUMENTED[1:]}",  # non-ASCII must not crash the compare
    f"t={'9' * 40},v1={DOCUMENTED}",  # an absurd timestamp
    "t=1790000000;v1=" + DOCUMENTED,  # the wrong separator
    "x" * 10_000,
]


@pytest.mark.parametrize("header", WRONG_HEADERS)
def test_a_missing_malformed_or_wrong_signature_is_refused(header: str | None) -> None:
    with pytest.raises(WebhookSignatureInvalid):
        check(header)


def test_every_failure_gives_the_same_error_so_a_probe_learns_nothing() -> None:
    seen = set()
    for header in [*WRONG_HEADERS, GOOD_HEADER]:
        try:
            check(header, now=NOW + timedelta(seconds=400))  # also stale
        except WebhookSignatureInvalid as error:
            seen.add((type(error), error.detail, error.status_code, tuple(error.headers().items())))
    assert len(seen) == 1
    ((_, _, status, headers),) = seen
    assert status == 401
    assert headers == (("WWW-Authenticate", 'Signature realm="webhook"'),)

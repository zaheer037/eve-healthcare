"""Passwords, access tokens and hashing slots (spec section 9.2)."""

import re
import threading
import time
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest

from app.core.config import Settings
from app.core.errors import InvalidToken, ServiceUnavailable
from app.core.security import (
    HashSlots,
    create_access_token,
    decode_access_token,
    dummy_hash,
    hash_password,
    jwt_api,
    verify_password,
)
from tests.support import SettingsFactory

PASSWORD = "correct horse battery staple"
ARGON2_PARAMETERS = re.compile(r"\$argon2id\$v=\d+\$m=\d+,t=\d+,p=\d+\$")


# ---------------------------------------------------------------- passwords


def test_a_hash_verifies_its_password_and_no_other() -> None:
    hashed = hash_password(PASSWORD)
    assert verify_password(PASSWORD, hashed) is True
    assert verify_password(PASSWORD + "x", hashed) is False


def test_the_hash_is_argon2id_and_carries_its_own_parameters() -> None:
    assert ARGON2_PARAMETERS.match(hash_password(PASSWORD))


def test_edge_spaces_are_part_of_the_password() -> None:
    hashed = hash_password("  spaced pass phrase  ")
    assert verify_password("  spaced pass phrase  ", hashed) is True
    assert verify_password("spaced pass phrase", hashed) is False  # never stripped


def test_two_unicode_forms_of_one_visible_password_give_one_hash() -> None:
    composed = "caf\N{LATIN SMALL LETTER E WITH ACUTE} password"
    decomposed = "cafe\N{COMBINING ACUTE ACCENT} password"
    assert composed != decomposed
    assert verify_password(decomposed, hash_password(composed)) is True


def test_nfc_keeps_compatibility_forms_apart() -> None:
    """NIST SP 800-63B-4 names NFC. NFKC would merge the ligature with the letters."""
    ligature = "\N{LATIN SMALL LIGATURE FI}nancial planning phrase"
    assert unicodedata.normalize("NFC", ligature) == ligature
    assert verify_password("financial planning phrase", hash_password(ligature)) is False


def test_the_dummy_hash_uses_the_parameters_of_a_real_hash() -> None:
    """Same parameters mean the same cost, so an unknown email takes as long as a wrong password."""
    real = ARGON2_PARAMETERS.match(hash_password(PASSWORD))
    dummy = ARGON2_PARAMETERS.match(dummy_hash())
    assert real is not None
    assert dummy is not None
    assert real.group(0) == dummy.group(0)


def test_no_password_matches_the_dummy_hash() -> None:
    assert verify_password(PASSWORD, dummy_hash()) is False


# ---------------------------------------------------------------- access tokens


@pytest.fixture
def settings(make_settings: SettingsFactory) -> Settings:
    return make_settings(environment="test", jwt_secret="s" * 40, access_token_minutes=30)


def mint(settings: Settings, **overrides: Any) -> str:
    """A token with chosen claims, signed like a real one."""
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "sub": str(uuid.UUID(int=1)),
        "iat": now,
        "exp": now + timedelta(minutes=30),
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
    }
    claims.update(overrides)
    claims = {name: value for name, value in claims.items() if value is not None}
    return jwt.encode(claims, settings.jwt_secret.get_secret_value(), algorithm="HS256")


def test_a_token_round_trips_to_the_user_id(settings: Settings) -> None:
    user_id = uuid.uuid4()
    token, expires_in = create_access_token(user_id, settings)
    assert decode_access_token(token, settings) == user_id
    assert expires_in == 1800


def test_a_token_holds_five_claims_and_no_role(settings: Settings) -> None:
    token, _ = create_access_token(uuid.UUID(int=5), settings)
    claims = jwt.decode(token, options={"verify_signature": False})
    assert set(claims) == {"sub", "iat", "exp", "iss", "aud"}
    assert claims["sub"] == str(uuid.UUID(int=5))
    assert claims["exp"] - claims["iat"] == 1800
    assert claims["iss"] == settings.jwt_issuer
    assert claims["aud"] == settings.jwt_audience


def test_the_lifetime_follows_the_setting(make_settings: SettingsFactory) -> None:
    settings = make_settings(environment="test", access_token_minutes=5)
    _, expires_in = create_access_token(uuid.uuid4(), settings)
    assert expires_in == 300


def test_an_expired_token_is_rejected(settings: Settings) -> None:
    past = datetime.now(UTC) - timedelta(minutes=5)
    token = mint(settings, iat=past - timedelta(minutes=30), exp=past)
    with pytest.raises(InvalidToken):
        decode_access_token(token, settings)


def test_a_token_issued_in_the_future_is_rejected(settings: Settings) -> None:
    later = datetime.now(UTC) + timedelta(hours=1)
    with pytest.raises(InvalidToken):
        decode_access_token(mint(settings, iat=later, exp=later + timedelta(minutes=30)), settings)


def test_alg_none_is_rejected(settings: Settings) -> None:
    claims = {
        "sub": str(uuid.uuid4()),
        "iat": datetime.now(UTC),
        "exp": datetime.now(UTC) + timedelta(minutes=30),
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
    }
    with pytest.raises(InvalidToken):
        decode_access_token(jwt.encode(claims, None, algorithm="none"), settings)


def test_a_token_signed_with_another_secret_is_rejected(settings: Settings) -> None:
    other = jwt.encode(
        {"sub": str(uuid.uuid4()), "exp": datetime.now(UTC) + timedelta(minutes=5)},
        "z" * 40,
        algorithm="HS256",
    )
    with pytest.raises(InvalidToken):
        decode_access_token(other, settings)


@pytest.mark.parametrize("claim", ["exp", "sub", "iss", "aud"])
def test_a_missing_required_claim_is_rejected(settings: Settings, claim: str) -> None:
    with pytest.raises(InvalidToken):
        decode_access_token(mint(settings, **{claim: None}), settings)


@pytest.mark.parametrize("claims", [{"iss": "someone-else"}, {"aud": "another-api"}])
def test_a_wrong_issuer_or_audience_is_rejected(settings: Settings, claims: dict[str, str]) -> None:
    with pytest.raises(InvalidToken):
        decode_access_token(mint(settings, **claims), settings)


@pytest.mark.parametrize("subject", [123, "not-a-uuid", ""])
def test_a_subject_that_is_not_a_uuid_string_is_rejected(settings: Settings, subject: Any) -> None:
    with pytest.raises(InvalidToken):
        decode_access_token(mint(settings, sub=subject), settings)


@pytest.mark.parametrize("garbage", ["", "not-a-jwt", "a.b.c", "Bearer x"])
def test_garbage_is_rejected(settings: Settings, garbage: str) -> None:
    with pytest.raises(InvalidToken):
        decode_access_token(garbage, settings)


def test_the_minimum_key_length_is_enforced() -> None:
    """PyJWT only warns about a short HMAC key unless the option is on (sharp edge 49)."""
    with pytest.raises(jwt.InvalidKeyError):
        jwt_api.encode({"sub": "x"}, "short", algorithm="HS256")


# ---------------------------------------------------------------- hashing slots


def test_a_slot_is_free_again_after_use() -> None:
    slots = HashSlots(1)
    with slots.slot():
        pass
    with slots.slot():
        pass


def test_a_slot_is_free_again_after_an_error() -> None:
    slots = HashSlots(1)
    with pytest.raises(RuntimeError), slots.slot():
        raise RuntimeError("the handler failed")
    with slots.slot():
        pass


def test_a_full_set_of_slots_turns_the_caller_away_at_once() -> None:
    slots = HashSlots(2)
    with slots.slot(), slots.slot():
        started = time.perf_counter()
        with pytest.raises(ServiceUnavailable) as refused, slots.slot():
            pytest.fail("a third caller must not get a slot")
        assert time.perf_counter() - started < 0.1  # it does not wait
    assert refused.value.retry_after == 1
    assert refused.value.status_code == 503


def test_never_more_slots_than_the_limit_run_at_once() -> None:
    limit, callers = 3, 12
    slots = HashSlots(limit)
    holding = threading.Event()
    lock = threading.Lock()
    running = peak = refused = 0

    def caller() -> str:
        nonlocal running, peak, refused
        try:
            with slots.slot():
                with lock:
                    running += 1
                    peak = max(peak, running)
                holding.wait(timeout=5)
                with lock:
                    running -= 1
                return "served"
        except ServiceUnavailable:
            with lock:
                refused += 1
            return "refused"

    with ThreadPoolExecutor(callers) as pool:
        futures = [pool.submit(caller) for _ in range(callers)]
        while True:  # wait until every caller that could not get a slot has been turned away
            with lock:
                if refused == callers - limit:
                    break
            time.sleep(0.005)
        holding.set()
        outcomes = [future.result() for future in futures]
    assert peak == limit
    assert outcomes.count("served") == limit
    assert outcomes.count("refused") == callers - limit

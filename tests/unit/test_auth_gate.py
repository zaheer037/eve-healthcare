"""The auth gate: throttle first, then a hashing slot held until the handler returns."""

import asyncio
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import Request

from app.auth.dependencies import auth_gate, client_ip
from app.core.errors import RateLimited, ServiceUnavailable
from app.core.security import HashSlots
from app.core.throttle import LoginThrottle
from tests.support import SettingsFactory


def fake_request(
    throttle: LoginThrottle, slots: HashSlots, host: str | None = "10.0.0.1"
) -> Request:
    state = SimpleNamespace(throttle=throttle, hash_slots=slots)
    client = SimpleNamespace(host=host) if host else None
    return cast(Request, SimpleNamespace(app=SimpleNamespace(state=state), client=client))


@pytest.fixture
def throttle(make_settings: SettingsFactory) -> LoginThrottle:
    return LoginThrottle(
        make_settings(
            environment="test",
            auth_ip_limit_per_minute=3,
            login_max_failures=2,
            login_failure_window_minutes=15,
        )
    )


def test_the_client_ip_is_the_address_in_the_scope(throttle: LoginThrottle) -> None:
    assert client_ip(fake_request(throttle, HashSlots(1), "203.0.113.7")) == "203.0.113.7"
    assert client_ip(fake_request(throttle, HashSlots(1), None)) == "unknown"


def test_the_gate_holds_a_slot_until_the_handler_returns(throttle: LoginThrottle) -> None:
    slots = HashSlots(1)
    request = fake_request(throttle, slots)

    async def scenario() -> None:
        async with auth_gate(request):
            with pytest.raises(ServiceUnavailable), slots.slot():  # the only slot is taken
                pytest.fail("the handler holds the slot")
        with slots.slot():  # released when the gate exits
            pass

    asyncio.run(scenario())


def test_the_gate_releases_the_slot_when_the_handler_fails(throttle: LoginThrottle) -> None:
    slots = HashSlots(1)
    request = fake_request(throttle, slots)

    async def scenario() -> None:
        with pytest.raises(RuntimeError):
            async with auth_gate(request):
                raise RuntimeError("the handler failed")
        with slots.slot():
            pass

    asyncio.run(scenario())


def test_the_gate_counts_the_request_against_the_ip_limit(throttle: LoginThrottle) -> None:
    request = fake_request(throttle, HashSlots(4))

    async def scenario() -> None:
        for _ in range(3):
            async with auth_gate(request):
                pass
        with pytest.raises(RateLimited):
            async with auth_gate(request):
                pytest.fail("the fourth request is over the limit")

    asyncio.run(scenario())


def test_the_gate_refuses_a_locked_account(throttle: LoginThrottle) -> None:
    request = fake_request(throttle, HashSlots(4))
    for _ in range(2):
        throttle.record_failure("jane@example.in")

    async def scenario() -> None:
        with pytest.raises(RateLimited):
            async with auth_gate(request, "Jane@Example.in"):
                pytest.fail("the account is locked")

    asyncio.run(scenario())


def test_a_throttled_request_never_takes_a_slot_so_it_hears_429_not_503(
    throttle: LoginThrottle,
) -> None:
    slots = HashSlots(1)
    request = fake_request(throttle, slots)
    for _ in range(3):
        throttle.check("10.0.0.1")  # the IP is now at its limit

    async def scenario() -> None:
        # Every slot is taken as well, and the throttle is asked first.
        with slots.slot(), pytest.raises(RateLimited):
            async with auth_gate(request):
                pytest.fail("the gate must refuse")

    asyncio.run(scenario())


def test_a_full_set_of_slots_answers_503_at_once(throttle: LoginThrottle) -> None:
    slots = HashSlots(1)
    request = fake_request(throttle, slots)

    async def scenario() -> ServiceUnavailable:
        with slots.slot(), pytest.raises(ServiceUnavailable) as refused:
            async with auth_gate(request):
                pytest.fail("no slot is free")
        return refused.value

    assert asyncio.run(scenario()).retry_after == 1

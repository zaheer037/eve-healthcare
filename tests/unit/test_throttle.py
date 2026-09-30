"""The login throttle: per IP and per account, with fixed-size keys (architecture §7.2)."""

import pytest
from limits.strategies import SlidingWindowCounterRateLimiter
from structlog.testing import capture_logs

from app.core.errors import RateLimited
from app.core.throttle import LoginThrottle, account_key, client_key
from tests.support import SettingsFactory


@pytest.fixture
def throttle(make_settings: SettingsFactory) -> LoginThrottle:
    settings = make_settings(
        environment="test",
        auth_ip_limit_per_minute=5,
        login_max_failures=3,
        login_failure_window_minutes=15,
    )
    return LoginThrottle(settings)


def test_the_ip_limit_counts_every_request(throttle: LoginThrottle) -> None:
    for _ in range(5):
        throttle.check("10.0.0.1")
    with pytest.raises(RateLimited) as refused:
        throttle.check("10.0.0.1")
    assert 1 <= (refused.value.retry_after or 0) <= 60
    assert refused.value.status_code == 429


def test_the_ip_limit_is_kept_per_address(throttle: LoginThrottle) -> None:
    for _ in range(5):
        throttle.check("10.0.0.1")
    throttle.check("10.0.0.2")  # another client is not affected


def test_requests_do_not_use_up_the_account_budget(throttle: LoginThrottle) -> None:
    """Only failures count against an account, so a busy IP cannot lock a user out by itself."""
    for _ in range(4):
        throttle.check("10.0.0.1", "jane@example.in")
    throttle.check("10.0.0.9", "jane@example.in")  # still allowed: no failure was recorded


def test_the_account_is_locked_after_the_allowed_failures(throttle: LoginThrottle) -> None:
    for _ in range(3):
        throttle.check("10.0.0.1", "jane@example.in")
        throttle.record_failure("jane@example.in")
    with pytest.raises(RateLimited) as refused:
        throttle.check("10.0.0.2", "jane@example.in")  # even from another address
    assert 1 <= (refused.value.retry_after or 0) <= 15 * 60


def test_a_good_login_clears_the_account_counter(throttle: LoginThrottle) -> None:
    for _ in range(2):
        throttle.record_failure("jane@example.in")
    throttle.reset("jane@example.in")
    for _ in range(2):
        throttle.record_failure("jane@example.in")
    throttle.check("10.0.0.1", "jane@example.in")  # two failures again, below the limit of three


def test_case_variants_share_one_counter(throttle: LoginThrottle) -> None:
    for spelling in ("Jane@Example.in", "JANE@EXAMPLE.IN", "jane@example.in"):
        throttle.record_failure(spelling)
    with pytest.raises(RateLimited):
        throttle.check("10.0.0.1", "jAnE@eXaMpLe.In")


def test_an_email_that_does_not_exist_is_throttled_like_one_that_does(
    throttle: LoginThrottle,
) -> None:
    """The throttle never asks whether the account exists, so it reveals nothing about it."""
    for _ in range(3):
        throttle.record_failure("nobody-registered-this@example.in")
    with pytest.raises(RateLimited):
        throttle.check("10.0.0.1", "nobody-registered-this@example.in")


def test_accounts_are_counted_apart(throttle: LoginThrottle) -> None:
    for _ in range(3):
        throttle.record_failure("jane@example.in")
    throttle.check("10.0.0.1", "john@example.in")


def test_the_account_key_has_a_fixed_size_for_any_email() -> None:
    assert len(account_key("a@example.in")) == 64
    assert len(account_key("a" * 100_000 + "@example.in")) == 64


def test_the_account_key_ignores_case() -> None:
    assert account_key("Jane@Example.in") == account_key("jane@example.in")
    assert account_key("jane@example.in") != account_key("john@example.in")


def test_the_account_key_ignores_the_unicode_form() -> None:
    composed = "caf\N{LATIN SMALL LETTER E WITH ACUTE}@example.in"
    decomposed = "cafe\N{COMBINING ACUTE ACCENT}@example.in"
    assert composed != decomposed
    assert account_key(composed) == account_key(decomposed)


# ==== the client key: one budget per client, not per address

IPV6_64 = "2001:db8:abcd:12"


@pytest.mark.parametrize(
    ("address", "key"),
    [
        ("203.0.113.7", "203.0.113.7"),
        (f"{IPV6_64}:1:2:3:4", f"{IPV6_64}::/64"),
        (f"{IPV6_64}:ffff:ffff:ffff:ffff", f"{IPV6_64}::/64"),
        ("::ffff:203.0.113.7", "203.0.113.7"),
        ("unknown", "unknown"),
        ("testclient", "testclient"),
    ],
)
def test_the_client_key_is_the_ipv4_address_or_the_ipv6_64(address: str, key: str) -> None:
    assert client_key(address) == key
    assert client_key(key) == key  # a key that goes through again stays the same


def test_every_address_of_one_ipv6_64_shares_one_ip_budget(throttle: LoginThrottle) -> None:
    """A client holds a whole /64, so a budget per address gave it 2^64 budgets."""
    for number in range(5):
        throttle.check(f"{IPV6_64}::{number:x}")
    with pytest.raises(RateLimited):
        throttle.check(f"{IPV6_64}:ffff::1")  # a fresh address in the same /64
    throttle.check("2001:db8:abcd:13::1")  # the next /64 has a budget of its own


def test_an_ipv4_mapped_address_shares_the_budget_of_the_ipv4_address(
    throttle: LoginThrottle,
) -> None:
    for _ in range(5):
        throttle.check("203.0.113.7")
    with pytest.raises(RateLimited):
        throttle.check("::ffff:203.0.113.7")


# ==== memory follows the traffic of the last window


def test_the_counters_expire_with_their_window(throttle: LoginThrottle) -> None:
    """The moving-window strategy of `limits` keeps a key for every address and email that it has
    ever seen, and each expiry pass walks them all. 20,000 keys were still there minutes after
    their window. The counter strategy deletes a key when its window is over."""
    assert isinstance(throttle._strategy, SlidingWindowCounterRateLimiter)


# ==== what an operator sees


def test_a_failure_and_each_kind_of_refusal_are_logged_and_never_with_the_email(
    throttle: LoginThrottle,
) -> None:
    with capture_logs() as logs:
        for _ in range(3):
            throttle.record_failure("jane@example.in")
        for _ in range(5):
            throttle.check("10.0.0.1")
        with pytest.raises(RateLimited):
            throttle.check("10.0.0.1")
        with pytest.raises(RateLimited):
            throttle.check("10.0.0.2", "jane@example.in")
    failures = [line for line in logs if line["event"] == "auth.login_failed"]
    refusals = [line for line in logs if line["event"] == "auth.throttled"]
    account = account_key("jane@example.in")[:12]
    assert [line["account"] for line in failures] == [account] * 3
    assert {line["log_level"] for line in failures + refusals} == {"warning"}
    by_ip, by_account = refusals
    assert (by_ip["scope"], by_ip["client"]) == ("ip", "10.0.0.1")
    assert (by_account["scope"], by_account["client"], by_account["account"]) == (
        "account",
        "10.0.0.2",
        account,
    )
    assert by_ip["retry_after"] >= 1
    assert "jane" not in str(logs)

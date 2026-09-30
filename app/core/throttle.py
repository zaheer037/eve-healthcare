"""The auth throttle: a limit per client and a limit on failed logins per account.

It uses the `limits` library with in-memory storage and a sliding window counter (architecture
§7.2). The counter strategy deletes a key when its window is over. The moving-window strategy never
does, so its memory grew with every address and email that it had seen, and each expiry pass walked
them all.
`# known limit: the counters are per process -> use Redis storage (limits[redis], with expiry)
before public exposure or with more than one worker`.
"""

import hashlib
import ipaddress
import math
import time
import unicodedata

import structlog
from limits import RateLimitItem, RateLimitItemPerMinute
from limits.storage import MemoryStorage
from limits.strategies import SlidingWindowCounterRateLimiter

from app.core.config import Settings
from app.core.errors import RateLimited

log = structlog.get_logger()

IPV6_PREFIX = 64  # the smallest network that a provider hands to one customer
ACCOUNT_LOG_LENGTH = 12  # hex characters of the account key that a log line shows


def account_key(email: str) -> str:
    """A fixed-size key for an email: the SHA-256 of its NFC-normalised, lower-cased form.

    Case variants share one counter, and an email of any length costs the same memory.
    """
    return hashlib.sha256(unicodedata.normalize("NFC", email).lower().encode()).hexdigest()


def client_key(address: str) -> str:
    """The key of the client behind `address`: its IPv4 address, or its IPv6 /64 network.

    A client that owns a /64 can send from 2^64 addresses, so a budget per address is no budget.
    An IPv4-mapped IPv6 address is the IPv4 address that it wraps. Anything that is not an IP
    address (`unknown`, or the name that a test client sends) is its own key.
    """
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return address
    if isinstance(parsed, ipaddress.IPv6Address):
        if parsed.ipv4_mapped is not None:
            return str(parsed.ipv4_mapped)
        return str(ipaddress.ip_network(f"{parsed}/{IPV6_PREFIX}", strict=False))
    return str(parsed)


class LoginThrottle:
    def __init__(self, settings: Settings) -> None:
        self._strategy = SlidingWindowCounterRateLimiter(MemoryStorage())
        # Every request from one client counts, across signup, login and token.
        self._ip_limit = RateLimitItemPerMinute(settings.auth_ip_limit_per_minute)
        # Only a failed login counts against an account.
        self._account_limit = RateLimitItemPerMinute(
            settings.login_max_failures, settings.login_failure_window_minutes
        )

    def check(self, client: str, email: str | None = None) -> None:
        """Count the request against the client limit. Refuse when the client or the account is
        spent. A refusal is logged at warning, with the scope and never with the email."""
        key = client_key(client)
        if not self._strategy.hit(self._ip_limit, "ip", key):
            retry_after = self._retry_after(self._ip_limit, "ip", key)
            log.warning("auth.throttled", scope="ip", client=key, retry_after=retry_after)
            raise RateLimited(retry_after=retry_after)
        if email is not None:
            account = account_key(email)
            if not self._strategy.test(self._account_limit, "account", account):
                retry_after = self._retry_after(self._account_limit, "account", account)
                log.warning(
                    "auth.throttled",
                    scope="account",
                    client=key,
                    account=account[:ACCOUNT_LOG_LENGTH],
                    retry_after=retry_after,
                )
                raise RateLimited(retry_after=retry_after)

    def record_failure(self, email: str) -> None:
        """Count one failed login. The event carries a short account key, so an operator sees a
        run against one account, and the client that the auth gate put into the log context."""
        account = account_key(email)
        self._strategy.hit(self._account_limit, "account", account)
        log.warning("auth.login_failed", account=account[:ACCOUNT_LOG_LENGTH])

    def reset(self, email: str) -> None:
        """A good login clears the account counter."""
        self._strategy.clear(self._account_limit, "account", account_key(email))

    def _retry_after(self, limit: RateLimitItem, *identifiers: str) -> int:
        stats = self._strategy.get_window_stats(limit, *identifiers)
        return max(1, math.ceil(stats.reset_time - time.time()))

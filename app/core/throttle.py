"""The auth throttle: a limit per client IP and a limit on failed logins per account.

It uses the `limits` library with in-memory storage and a moving window (architecture §7.2).
`# ponytail: in-memory counters are per process and only grow -> use Redis storage
(limits[redis], with expiry) before public exposure or with more than one worker`.
"""

import hashlib
import math
import time
import unicodedata

from limits import RateLimitItem, RateLimitItemPerMinute
from limits.storage import MemoryStorage
from limits.strategies import MovingWindowRateLimiter

from app.core.config import Settings
from app.core.errors import RateLimited


def account_key(email: str) -> str:
    """A fixed-size key for an email: the SHA-256 of its NFC-normalised, lower-cased form.

    Case variants share one counter, and an email of any length costs the same memory.
    """
    return hashlib.sha256(unicodedata.normalize("NFC", email).lower().encode()).hexdigest()


class LoginThrottle:
    def __init__(self, settings: Settings) -> None:
        self._strategy = MovingWindowRateLimiter(MemoryStorage())
        # Every request from one IP counts, across signup, login and token.
        self._ip_limit = RateLimitItemPerMinute(settings.auth_ip_limit_per_minute)
        # Only a failed login counts against an account.
        self._account_limit = RateLimitItemPerMinute(
            settings.login_max_failures, settings.login_failure_window_minutes
        )

    def check(self, ip: str, email: str | None = None) -> None:
        """Count the request against the IP limit. Refuse when the IP or the account is spent."""
        if not self._strategy.hit(self._ip_limit, "ip", ip):
            raise RateLimited(retry_after=self._retry_after(self._ip_limit, "ip", ip))
        if email is not None:
            key = account_key(email)
            if not self._strategy.test(self._account_limit, "account", key):
                raise RateLimited(
                    retry_after=self._retry_after(self._account_limit, "account", key)
                )

    def record_failure(self, email: str) -> None:
        self._strategy.hit(self._account_limit, "account", account_key(email))

    def reset(self, email: str) -> None:
        """A good login clears the account counter."""
        self._strategy.clear(self._account_limit, "account", account_key(email))

    def _retry_after(self, limit: RateLimitItem, *identifiers: str) -> int:
        stats = self._strategy.get_window_stats(limit, *identifiers)
        return max(1, math.ceil(stats.reset_time - time.time()))

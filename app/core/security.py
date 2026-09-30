"""Passwords, access tokens and hashing slots (spec section 9.2)."""

import secrets
import threading
import unicodedata
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from functools import cache

import jwt
from pwdlib import PasswordHash

from app.core.config import Settings
from app.core.errors import InvalidToken, ServiceUnavailable

ALGORITHM = "HS256"
REQUIRED_CLAIMS = ["exp", "sub", "iss", "aud"]

# Argon2id with the library defaults (64 MiB, 3 passes, 4 lanes). The hash string carries its own
# parameters, so the cost can rise later and old hashes still verify.
_password_hash = PasswordHash.recommended()

# PyJWT only warns about an HMAC key under 32 bytes unless this option is on (sharp edge 49).
jwt_api = jwt.PyJWT(options={"enforce_minimum_key_length": True})


def _nfc(password: str) -> str:
    """NIST SP 800-63B-4 names NFC. The password is never stripped: a space is part of it."""
    return unicodedata.normalize("NFC", password)


def hash_password(plain: str) -> str:
    return _password_hash.hash(_nfc(plain))


def verify_password(plain: str, hashed: str) -> bool:
    return _password_hash.verify(_nfc(plain), hashed)


@cache
def dummy_hash() -> str:
    """A real hash of a random password, made once per process with the real hasher.

    An unknown email is verified against it, so it costs as much as a wrong password. The app
    asks for it at start-up, so the 60 ms are paid there and not by the first unknown login.
    """
    return hash_password(secrets.token_urlsafe(24))


def create_access_token(user_id: uuid.UUID, settings: Settings) -> tuple[str, int]:
    """Return the token and its lifetime in seconds.

    The token uses the real clock, because PyJWT checks `iat` and `exp` against it (sharp edge 40).
    It holds no role: the service reads the role from the database, so a role change is instant.
    """
    issued_at = datetime.now(UTC)
    lifetime = timedelta(minutes=settings.access_token_minutes)
    claims = {
        "sub": str(user_id),
        "iat": issued_at,
        "exp": issued_at + lifetime,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
    }
    token = jwt_api.encode(claims, settings.jwt_secret.get_secret_value(), algorithm=ALGORITHM)
    return token, int(lifetime.total_seconds())


def decode_access_token(token: str, settings: Settings) -> uuid.UUID:
    """Return the user ID, or raise `InvalidToken`. One error for every reason.

    A probe learns nothing from the difference between a bad signature and an expired token.

    The algorithm is pinned. PyJWT checks `exp`, `iat`, the issuer and the audience.
    """
    try:
        claims = jwt_api.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[ALGORITHM],
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            options={"require": REQUIRED_CLAIMS},
        )
        return uuid.UUID(claims["sub"])
    except (jwt.InvalidTokenError, ValueError) as error:  # a bad UUID string is a ValueError
        raise InvalidToken from error


class HashSlots:
    """A fixed number of password hashes may run at once. Nobody waits for a slot.

    A hash costs CPU and 64 MiB. A request that waits for a slot inside a pool thread would hold
    that thread, and a burst of logins then starves the pool (spec sharp edge 43). So a caller
    takes a slot without blocking, on the event loop, and gets a 503 at once when all are taken.
    """

    def __init__(self, size: int) -> None:
        self._semaphore = threading.BoundedSemaphore(size)

    @contextmanager
    def slot(self) -> Iterator[None]:
        if not self._semaphore.acquire(blocking=False):
            raise ServiceUnavailable("Too many sign-in requests are running. Try again shortly.")
        try:
            yield
        finally:
            self._semaphore.release()

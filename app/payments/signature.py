"""Webhook signatures (architecture §6.5). Pure functions over the standard library.

The header is `X-Webhook-Signature: t=<unix seconds>,v1=<hex>`, and `v1` is
`HMAC_SHA256(secret, f"{t}." + raw_body)`. The signature covers the raw bytes, so it must be
verified before the body is parsed, and any re-formatting of the JSON breaks it.
"""

import hashlib
import hmac
import re
from datetime import datetime

from app.core.errors import WebhookSignatureInvalid

DEFAULT_TOLERANCE_SECONDS = 300
# ASCII only: `\\d` would also match digits of other scripts, and `int()` would accept them.
_TIMESTAMP = re.compile(r"[0-9]{1,12}")
_HEX_DIGEST = re.compile(r"[0-9a-fA-F]{64}")
_REFUSED = "The webhook signature is missing, malformed, wrong or stale."


def sign(secret: str, timestamp: int, body: bytes) -> str:
    """The `v1` value: the hex HMAC-SHA256 of `"<timestamp>." + body`."""
    message = f"{timestamp}.".encode() + body
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def header_for(secret: str, timestamp: int, body: bytes) -> str:
    """The whole `X-Webhook-Signature` value. The provider script uses it."""
    return f"t={timestamp},v1={sign(secret, timestamp, body)}"


def _parse(header: str | None) -> tuple[int, str]:
    """Split the header into its timestamp and its digest, or refuse it.

    A part that this version does not know is ignored, so the provider can add one. A part that
    appears twice is refused, because two answers to one question hide a mistake.
    """
    if not header:
        raise WebhookSignatureInvalid(_REFUSED)
    parts: dict[str, str] = {}
    for part in header.split(","):
        key, separator, value = part.strip().partition("=")
        if key not in ("t", "v1"):
            continue
        if not separator or key in parts:
            raise WebhookSignatureInvalid(_REFUSED)
        parts[key] = value
    timestamp, digest = parts.get("t", ""), parts.get("v1", "")
    if not _TIMESTAMP.fullmatch(timestamp) or not _HEX_DIGEST.fullmatch(digest):
        raise WebhookSignatureInvalid(_REFUSED)
    return int(timestamp), digest


def verify(
    secret: str,
    header: str | None,
    body: bytes,
    *,
    now: datetime,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
) -> None:
    """Raise `WebhookSignatureInvalid` unless the header signs this body and is fresh.

    One error for a missing header, a malformed one, a wrong digest and a stale timestamp, so a
    probe learns nothing from the difference. The digests are compared as bytes in constant time.
    """
    timestamp, supplied = _parse(header)
    if abs(now.timestamp() - timestamp) > tolerance_seconds:
        raise WebhookSignatureInvalid(_REFUSED)
    expected = sign(secret, timestamp, body)
    if not hmac.compare_digest(expected.encode(), supplied.lower().encode()):
        raise WebhookSignatureInvalid(_REFUSED)

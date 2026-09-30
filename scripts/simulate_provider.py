"""A stand-in for the payment provider: it sends signed webhook events to the API.

    uv run python -m scripts.simulate_provider --payment-id <PAYMENT_ID> --result SUCCESS \\
        --repeat 5 --concurrent

Every run makes a fresh event ID. `--repeat` sends that event again, so the copies are duplicates,
and `--concurrent` sends the copies at the same moment. Like a real provider, it retries a network
error and a 5xx (also 408 and 429) with exponential backoff and jitter. It stops at once on a 2xx
and on any other 4xx, because a request that is wrong stays wrong (architecture §6.4). The secret
comes from `--secret`, the `WEBHOOK_SECRET` variable or `.env`, and is never printed.
"""

import argparse
import http.client
import json
import random
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import urlsplit

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.payments.signature import header_for

DEFAULT_URL = "http://localhost:8000/payments/webhook/"
RETRIED_STATUSES = frozenset({408, 429})  # and every 5xx
BACKOFF_BASE_SECONDS = 0.2
BACKOFF_CAP_SECONDS = 5.0
DEFAULT_ATTEMPTS = 5
REQUEST_TIMEOUT_SECONDS = 10.0
SERVER_ERROR = 500
OK_STATUSES = range(200, 300)


class SecretSource(BaseSettings):
    """Where the secret comes from when `--secret` is absent: the `WEBHOOK_SECRET` variable, then
    the `.env` file. It is the secret that the API reads, so the two always agree."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    webhook_secret: SecretStr | None = None


@dataclass(frozen=True, slots=True)
class Reply:
    """What came back. `status` is `None` when nothing did (a network error)."""

    status: int | None
    body: str


@dataclass(frozen=True, slots=True)
class Delivery:
    """The end of one delivery: how many attempts it took and the last reply."""

    attempts: int
    reply: Reply

    @property
    def succeeded(self) -> bool:
        return self.reply.status in OK_STATUSES


type Transport = Callable[[str, bytes, Mapping[str, str]], Reply]


def http_send(
    url: str,
    body: bytes,
    headers: Mapping[str, str],
    *,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
) -> Reply:
    """POST the bytes. An error status is a reply, and a network error is a reply without a
    status, so the caller decides about the retry. The standard library keeps the script free of
    a dependency that the runtime image does not have."""
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")  # noqa: S310  # main() accepts only http and https
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return Reply(response.status, response.read().decode(errors="replace"))
    except urllib.error.HTTPError as error:  # a 4xx or a 5xx
        return Reply(error.code, error.read().decode(errors="replace"))
    except (OSError, http.client.HTTPException) as error:  # refused, reset, timed out
        return Reply(None, f"{type(error).__name__}: {error}")


def build_event(  # noqa: PLR0913  # one keyword per field of the event
    payment_id: uuid.UUID,
    result: str,
    *,
    event_id: str | None = None,
    reference: str | None = None,
    amount: str | None = None,
    currency: str | None = None,
    failure_reason: str | None = None,
) -> bytes:
    """The body of one event, as compact JSON. The signature covers these exact bytes."""
    event = {
        "event_id": event_id or f"evt_{uuid.uuid4().hex}",
        "payment_id": str(payment_id),
        "status": result,
        "provider_payment_id": reference or f"sim_{payment_id.hex}",
    }
    optional = {"amount": amount, "currency": currency, "failure_reason": failure_reason}
    event.update({key: value for key, value in optional.items() if value is not None})
    return json.dumps(event, separators=(",", ":")).encode()


def backoff(
    attempt: int,
    *,
    base: float = BACKOFF_BASE_SECONDS,
    cap: float = BACKOFF_CAP_SECONDS,
    jitter: Callable[[], float] = random.random,
) -> float:
    """The wait after a failed attempt: exponential, capped, with full jitter.

    A random point below the ceiling stops many providers from retrying in step.
    `# ponytail: Retry-After is not honoured -> wait at least as long as the server asks`.
    """
    return jitter() * min(cap, base * 2.0 ** (attempt - 1))


def worth_retrying(reply: Reply) -> bool:
    """Can another try help? A network error, a server error, a timeout or a rate limit can pass.

    A request that is wrong stays wrong: a 401, a 404, a 413 or a 422 gets the same answer again.
    """
    status = reply.status
    return status is None or status >= SERVER_ERROR or status in RETRIED_STATUSES


def deliver(  # noqa: PLR0913  # the transport, the clock and the dice are injected for tests
    url: str,
    secret: str,
    body: bytes,
    *,
    send: Transport = http_send,
    max_attempts: int = DEFAULT_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
    jitter: Callable[[], float] = random.random,
) -> Delivery:
    """Deliver one event, and retry what is worth a retry.

    Every attempt signs again with the current time, so a long backoff leaves no stale timestamp.
    The bytes, and so the event ID, stay the same, which is what makes a retry safe
    (architecture §6.2).
    """
    attempt = 0
    while True:
        attempt += 1
        headers = {
            "Content-Type": "application/json",
            "X-Webhook-Signature": header_for(secret, int(now()), body),
        }
        reply = send(url, body, headers)
        if not worth_retrying(reply) or attempt >= max_attempts:
            return Delivery(attempt, reply)
        sleep(backoff(attempt, jitter=jitter))


def deliver_many(  # noqa: PLR0913
    url: str,
    secret: str,
    body: bytes,
    *,
    repeat: int,
    concurrent: bool,
    send: Transport = http_send,
    max_attempts: int = DEFAULT_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
    jitter: Callable[[], float] = random.random,
) -> list[Delivery]:
    """Send the same event `repeat` times, one after another or all at the same moment."""

    def one() -> Delivery:
        return deliver(
            url,
            secret,
            body,
            send=send,
            max_attempts=max_attempts,
            sleep=sleep,
            now=now,
            jitter=jitter,
        )

    if not concurrent or repeat == 1:
        return [one() for _ in range(repeat)]
    start_together = threading.Barrier(repeat)  # a race is a race, not a timing accident

    def released() -> Delivery:
        start_together.wait()
        return one()

    with ThreadPoolExecutor(repeat) as pool:
        futures = [pool.submit(released) for _ in range(repeat)]
        return [future.result() for future in futures]


def _secret_from_settings() -> str | None:
    secret = SecretSource().webhook_secret
    return secret.get_secret_value() if secret else None


def _http_url(value: str) -> str:
    if urlsplit(value).scheme not in ("http", "https"):
        raise argparse.ArgumentTypeError("the URL must start with http:// or https://")
    return value


def _at_least_one(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return number


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.simulate_provider",
        description="Send signed webhook events, with duplicates and retries.",
    )
    parser.add_argument(
        "--payment-id",
        type=uuid.UUID,
        required=True,
        help="the payment to settle (from POST /payments/)",
    )
    parser.add_argument(
        "--result", choices=("SUCCESS", "FAILED"), default="SUCCESS", help="the status of the event"
    )
    parser.add_argument(
        "--url",
        type=_http_url,
        default=DEFAULT_URL,
        help=f"the full webhook URL, not the base (default: {DEFAULT_URL})",
    )
    parser.add_argument(
        "--secret", default=None, help="default: WEBHOOK_SECRET from the environment or .env"
    )
    parser.add_argument("--event-id", default=None, help="default: a fresh ID for every run")
    parser.add_argument("--reference", default=None, help="default: sim_<payment id>")
    parser.add_argument(
        "--amount", default=None, help="add an amount, for example 380.00 (a wrong one is rejected)"
    )
    parser.add_argument("--currency", default=None, help="add a currency, for example INR")
    parser.add_argument(
        "--failure-reason", default=None, help="add a reason (the API refuses one on SUCCESS)"
    )
    parser.add_argument("--repeat", type=_at_least_one, default=1, help="send the event N times")
    parser.add_argument("--concurrent", action="store_true", help="send the copies at once")
    parser.add_argument("--max-attempts", type=_at_least_one, default=DEFAULT_ATTEMPTS)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    send: Transport = http_send,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Send the events. Exit 0 when every delivery ended in a 2xx, and 1 otherwise."""
    parser = _parser()
    args = parser.parse_args(argv)
    secret = args.secret or _secret_from_settings()
    if not secret:
        parser.error("set --secret, or WEBHOOK_SECRET in the environment or in .env")
    body = build_event(
        args.payment_id,
        args.result,
        event_id=args.event_id,
        reference=args.reference,
        amount=args.amount,
        currency=args.currency,
        failure_reason=args.failure_reason,
    )
    print(f"event {json.loads(body)['event_id']} for payment {args.payment_id}")
    deliveries = deliver_many(
        args.url,
        secret,
        body,
        repeat=args.repeat,
        concurrent=args.concurrent,
        send=send,
        max_attempts=args.max_attempts,
        sleep=sleep,
    )
    for number, delivery in enumerate(deliveries, start=1):
        status = delivery.reply.status if delivery.reply.status is not None else "no answer"
        attempts = f"{delivery.attempts} attempt(s)"
        print(f"delivery {number}: HTTP {status} after {attempts}: {delivery.reply.body}")
    return 0 if all(delivery.succeeded for delivery in deliveries) else 1


if __name__ == "__main__":
    raise SystemExit(main())

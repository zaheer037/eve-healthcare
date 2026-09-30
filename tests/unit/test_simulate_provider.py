"""The provider simulator script: a signed sender with duplicates and retries (spec section 8.4)."""

import json
import threading
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from app.payments.signature import verify
from scripts.simulate_provider import (
    Delivery,
    Reply,
    backoff,
    build_event,
    deliver,
    deliver_many,
    http_send,
    main,
)

SECRET = "whsec_demo_only_not_a_real_secret_0123456789"
URL = "http://provider.test/payments/webhook/"
PAYMENT_ID = uuid.UUID("0d4b7a52-3f1e-4d0a-9b8c-2f6a1c7e9d10")
OK = Reply(200, '{"status":"processed","outcome":"APPLIED","detail":null}')


class FakeNetwork:
    """A transport that plays a script of replies and records every request."""

    def __init__(self, *replies: Reply) -> None:
        self.replies = list(replies)
        self.requests: list[tuple[str, bytes, Mapping[str, str]]] = []
        self.lock = threading.Lock()

    def __call__(self, url: str, body: bytes, headers: Mapping[str, str]) -> Reply:
        with self.lock:
            self.requests.append((url, body, headers))
            return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


class FakeClock:
    """Time that only moves when the script sleeps. `slowness` is extra time that each wait costs,
    as when the network is slow."""

    def __init__(self, start: float = 1_790_000_000.0, slowness: float = 0.0) -> None:
        self.now = start
        self.slowness = slowness
        self.sleeps: list[float] = []
        self.sent_at: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds + self.slowness

    def stamp(self) -> None:
        self.sent_at.append(self.now)

    def time(self) -> float:
        return self.now


def send_one(network: FakeNetwork, clock: FakeClock, **options: Any) -> Delivery:
    body = build_event(PAYMENT_ID, "SUCCESS", event_id="evt_1")
    return deliver(
        URL,
        SECRET,
        body,
        send=network,
        sleep=clock.sleep,
        now=clock.time,
        jitter=lambda: 1.0,  # the longest wait, so a test sees the whole schedule
        **options,
    )


# ==== the event


def test_an_event_is_compact_json_because_the_signature_covers_the_bytes() -> None:
    body = build_event(PAYMENT_ID, "SUCCESS", event_id="evt_1")
    assert b" " not in body
    assert json.loads(body) == {
        "event_id": "evt_1",
        "payment_id": str(PAYMENT_ID),
        "status": "SUCCESS",
        "provider_payment_id": f"sim_{PAYMENT_ID.hex}",
    }


def test_an_event_gets_a_fresh_id_unless_one_is_given() -> None:
    first = json.loads(build_event(PAYMENT_ID, "SUCCESS"))["event_id"]
    second = json.loads(build_event(PAYMENT_ID, "SUCCESS"))["event_id"]
    assert first != second
    assert first.startswith("evt_")


def test_the_optional_fields_appear_only_when_given() -> None:
    body = json.loads(
        build_event(
            PAYMENT_ID,
            "FAILED",
            event_id="evt_2",
            reference="ref_9",
            amount="499.00",
            currency="INR",
            failure_reason="declined",
        )
    )
    assert body["provider_payment_id"] == "ref_9"
    assert (body["amount"], body["currency"], body["failure_reason"]) == (
        "499.00",
        "INR",
        "declined",
    )
    plain = json.loads(build_event(PAYMENT_ID, "SUCCESS"))
    assert not {"amount", "currency", "failure_reason"} & set(plain)


# ==== the wait between attempts


@pytest.mark.parametrize(("attempt", "longest"), [(1, 0.2), (2, 0.4), (3, 0.8), (4, 1.6)])
def test_the_wait_doubles_and_the_jitter_picks_a_point_below_it(
    attempt: int, longest: float
) -> None:
    assert backoff(attempt, jitter=lambda: 1.0) == pytest.approx(longest)
    assert backoff(attempt, jitter=lambda: 0.0) == 0.0
    assert backoff(attempt, jitter=lambda: 0.5) == pytest.approx(longest / 2)


def test_the_wait_never_passes_the_cap() -> None:
    assert backoff(30, jitter=lambda: 1.0) == 5.0


# ==== delivery and retries


def test_a_first_answer_of_200_is_one_attempt_and_no_wait() -> None:
    network, clock = FakeNetwork(OK), FakeClock()
    delivery = send_one(network, clock)
    assert (delivery.attempts, delivery.reply.status, delivery.succeeded) == (1, 200, True)
    assert clock.sleeps == []


def test_a_503_is_retried_with_a_growing_wait_until_it_works() -> None:
    network, clock = FakeNetwork(Reply(503, "busy"), Reply(503, "busy"), OK), FakeClock()
    delivery = send_one(network, clock)
    assert (delivery.attempts, delivery.reply.status) == (3, 200)
    assert clock.sleeps == pytest.approx([0.2, 0.4])


def test_a_network_error_is_retried_too() -> None:
    network, clock = FakeNetwork(Reply(None, "connection refused"), OK), FakeClock()
    delivery = send_one(network, clock)
    assert (delivery.attempts, delivery.succeeded) == (2, True)


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_these_statuses_are_worth_a_retry(status: int) -> None:
    network, clock = FakeNetwork(Reply(status, "later"), OK), FakeClock()
    assert send_one(network, clock).attempts == 2


@pytest.mark.parametrize("status", [400, 401, 404, 413, 422])
def test_a_request_that_is_wrong_is_not_retried(status: int) -> None:
    """401, 413 and 422 mean the request itself is wrong, so a retry of the same bytes fails."""
    network, clock = FakeNetwork(Reply(status, "no"), OK), FakeClock()
    delivery = send_one(network, clock)
    assert (delivery.attempts, delivery.reply.status, delivery.succeeded) == (1, status, False)
    assert len(network.requests) == 1
    assert clock.sleeps == []


def test_it_gives_up_after_the_last_attempt_and_reports_the_last_answer() -> None:
    network, clock = FakeNetwork(Reply(503, "still busy")), FakeClock()
    delivery = send_one(network, clock, max_attempts=4)
    assert (delivery.attempts, delivery.reply.status, delivery.succeeded) == (4, 503, False)
    assert len(clock.sleeps) == 3  # no wait after the last attempt


def test_every_attempt_is_signed_afresh_over_the_same_bytes() -> None:
    """A slow retry must not leave a stale timestamp, and the event id must not change."""
    clock = FakeClock(slowness=30.0)  # every wait costs half a minute more than it says
    stamping = FakeNetwork(Reply(503, "busy"), OK)
    original_call = stamping.__call__

    def stamped(url: str, body: bytes, headers: Mapping[str, str]) -> Reply:
        clock.stamp()
        return original_call(url, body, headers)

    send_one(stamped, clock)  # type: ignore[arg-type]
    (_, first_body, first_headers), (_, second_body, second_headers) = stamping.requests
    assert first_body == second_body
    assert first_headers["X-Webhook-Signature"] != second_headers["X-Webhook-Signature"]
    for body, headers, sent_at in [
        (first_body, first_headers, clock.sent_at[0]),
        (second_body, second_headers, clock.sent_at[1]),
    ]:
        verify(  # each header is fresh at the moment it was sent, and the window is one second
            SECRET,
            headers["X-Webhook-Signature"],
            body,
            now=datetime.fromtimestamp(sent_at, tz=UTC),
            tolerance_seconds=1,
        )
    assert clock.sent_at[1] - clock.sent_at[0] > 30
    assert first_headers["Content-Type"] == "application/json"


# ==== duplicates


def test_repeat_sends_the_same_event_again_and_again() -> None:
    network, clock = FakeNetwork(OK), FakeClock()
    body = build_event(PAYMENT_ID, "SUCCESS", event_id="evt_1")
    deliveries = deliver_many(
        URL,
        SECRET,
        body,
        repeat=3,
        concurrent=False,
        send=network,
        sleep=clock.sleep,
        now=clock.time,
        jitter=lambda: 1.0,
    )
    assert len(deliveries) == 3
    assert [request[1] for request in network.requests] == [body] * 3


def test_concurrent_sends_them_at_the_same_moment() -> None:
    barrier = threading.Barrier(4, timeout=5)  # four calls must overlap, or this never passes

    def meeting(url: str, body: bytes, headers: Mapping[str, str]) -> Reply:
        barrier.wait()
        return OK

    deliveries = deliver_many(
        URL,
        SECRET,
        build_event(PAYMENT_ID, "SUCCESS", event_id="evt_1"),
        repeat=4,
        concurrent=True,
        send=meeting,
    )
    assert [delivery.succeeded for delivery in deliveries] == [True] * 4


# ==== the command line


def run(
    capsys: pytest.CaptureFixture[str], network: FakeNetwork, *arguments: str
) -> tuple[int, str]:
    code = main(
        ["--payment-id", str(PAYMENT_ID), "--secret", SECRET, "--url", URL, *arguments],
        send=network,
        sleep=lambda _: None,
    )
    return code, capsys.readouterr().out


def test_the_command_reports_each_delivery_and_exits_zero_when_all_succeed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, output = run(capsys, FakeNetwork(OK), "--repeat", "2", "--result", "SUCCESS")
    assert code == 0
    assert output.count("HTTP 200") == 2
    assert SECRET not in output  # a secret is never printed


def test_the_command_exits_one_when_a_delivery_fails(capsys: pytest.CaptureFixture[str]) -> None:
    code, output = run(capsys, FakeNetwork(Reply(401, "bad signature")))
    assert code == 1
    assert "HTTP 401" in output


def test_the_command_takes_the_secret_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)  # no .env here
    monkeypatch.setenv("WEBHOOK_SECRET", SECRET)
    network = FakeNetwork(OK)
    assert main(["--payment-id", str(PAYMENT_ID), "--url", URL], send=network) == 0
    (_, body, headers), *_ = network.requests
    verify(
        SECRET,
        headers["X-Webhook-Signature"],
        body,
        now=datetime.now(UTC),
    )  # signed with the secret from the environment


def test_the_command_reads_the_secret_from_a_dotenv_file_and_the_environment_wins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".env").write_text("WEBHOOK_SECRET=from-the-dotenv-file-0123456789abcdef0123\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WEBHOOK_SECRET", raising=False)
    network = FakeNetwork(OK)
    assert main(["--payment-id", str(PAYMENT_ID), "--url", URL], send=network) == 0
    (_, body, headers), *_ = network.requests
    verify(
        "from-the-dotenv-file-0123456789abcdef0123",
        headers["X-Webhook-Signature"],
        body,
        now=datetime.now(UTC),
    )
    monkeypatch.setenv("WEBHOOK_SECRET", SECRET)  # a variable beats the file
    other = FakeNetwork(OK)
    main(["--payment-id", str(PAYMENT_ID), "--url", URL], send=other)
    (_, other_body, other_headers), *_ = other.requests
    verify(SECRET, other_headers["X-Webhook-Signature"], other_body, now=datetime.now(UTC))


def test_the_command_refuses_to_run_without_a_secret(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)  # no .env here
    monkeypatch.delenv("WEBHOOK_SECRET", raising=False)
    with pytest.raises(SystemExit) as refused:
        main(["--payment-id", str(PAYMENT_ID), "--url", URL], send=FakeNetwork(OK))
    assert refused.value.code == 2
    assert "WEBHOOK_SECRET" in capsys.readouterr().err


def test_the_command_refuses_a_url_that_is_not_http(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as refused:
        main(
            ["--payment-id", str(PAYMENT_ID), "--secret", SECRET, "--url", "file:///etc/passwd"],
            send=FakeNetwork(OK),
        )
    assert refused.value.code == 2


def test_the_command_refuses_a_payment_id_that_is_not_a_uuid() -> None:
    with pytest.raises(SystemExit) as refused:
        main(["--payment-id", "nope", "--secret", SECRET], send=FakeNetwork(OK))
    assert refused.value.code == 2


# ==== the real transport, against a tiny local server


class Answering(BaseHTTPRequestHandler):
    status = 200

    def do_POST(self) -> None:  # the name is fixed by http.server
        length = int(self.headers["Content-Length"])
        self.server.received = (self.path, self.rfile.read(length), dict(self.headers))  # type: ignore[attr-defined]
        payload = b'{"status":"processed"}'
        self.send_response(self.server.reply_status)  # type: ignore[attr-defined]
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002  # silence the test run
        return


@pytest.fixture
def local_server() -> Any:
    server = HTTPServer(("127.0.0.1", 0), Answering)
    server.reply_status = 200  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_the_real_transport_posts_the_bytes_with_the_headers(local_server: Any) -> None:
    url = f"http://127.0.0.1:{local_server.server_port}/payments/webhook/"
    reply = http_send(
        url, b'{"a":1}', {"Content-Type": "application/json", "X-Webhook-Signature": "s"}
    )
    assert (reply.status, reply.body) == (200, '{"status":"processed"}')
    path, body, headers = local_server.received
    assert (path, body) == ("/payments/webhook/", b'{"a":1}')
    assert headers["X-Webhook-Signature"] == "s"


def test_the_real_transport_reports_an_error_status_without_raising(local_server: Any) -> None:
    local_server.reply_status = 503
    url = f"http://127.0.0.1:{local_server.server_port}/x"
    assert http_send(url, b"{}", {}).status == 503


def test_the_real_transport_reports_no_answer_as_a_reply_without_a_status() -> None:
    reply = http_send("http://127.0.0.1:1/unreachable", b"{}", {}, timeout=1)
    assert reply.status is None
    assert reply.body

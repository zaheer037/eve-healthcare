"""Logging: JSON output, redaction, no locals, scrubbed database errors, Uvicorn loggers."""

import json
import logging
import sys
from types import SimpleNamespace
from typing import Any

import pytest
import structlog
from sqlalchemy.exc import IntegrityError

from app.core.logging import DropTraceback, configure_logging
from tests.support import SettingsFactory


def emitted(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    lines = capsys.readouterr().out.splitlines()
    return [json.loads(line) for line in lines]


def test_json_lines_carry_timestamp_level_and_event(
    make_settings: SettingsFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(make_settings())
    structlog.get_logger().info("booking.created", booking_id="b-1")
    (line,) = emitted(capsys)
    assert line["event"] == "booking.created"
    assert line["level"] == "info"
    assert line["booking_id"] == "b-1"
    assert line["timestamp"].endswith("Z")


def test_the_level_setting_filters_events(
    make_settings: SettingsFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(make_settings(log_level="WARNING"))
    log = structlog.get_logger()
    log.info("dropped")
    log.warning("kept")
    assert [line["event"] for line in emitted(capsys)] == ["kept"]


def test_keys_that_look_like_secrets_are_removed_at_any_depth(
    make_settings: SettingsFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(make_settings())
    structlog.get_logger().info(
        "event",
        password="hunter2",
        access_token="abc",
        request={"Authorization": "Bearer abc", "path": "/x"},
        headers=[{"X-Webhook-Signature": "t=1,v1=ff", "ok": 1}],
        webhook_secret="s3cret",
        safe="kept",
    )
    (line,) = emitted(capsys)
    assert line["safe"] == "kept"
    assert line["request"] == {"path": "/x"}
    assert line["headers"] == [{"ok": 1}]
    for forbidden in ("hunter2", "abc", "s3cret", "v1=ff"):
        assert forbidden not in json.dumps(line)


def fail_with_a_secret_in_a_local() -> None:
    local_password = "correct-horse-battery-staple"  # noqa: F841 - a local, as in a real handler
    raise RuntimeError("unexpected failure")


def test_local_variables_stay_out_of_the_exception_log(
    make_settings: SettingsFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(make_settings())
    try:
        fail_with_a_secret_in_a_local()
    except RuntimeError:
        structlog.get_logger().exception("http.error")
    output = capsys.readouterr().out
    assert "correct-horse-battery-staple" not in output
    assert "RuntimeError" in output  # the error itself is still logged


class FakePostgresError(Exception):
    """What psycopg raises: the message echoes the row, and `diag` names the constraint."""

    sqlstate = "23505"

    def __init__(self) -> None:
        super().__init__("duplicate key value violates unique constraint. Key (email)=(a@x.in)")
        self.diag = SimpleNamespace(constraint_name="uq_users_email")


def a_database_error() -> IntegrityError:
    return IntegrityError("INSERT INTO users ...", {"email": "a@x.in"}, FakePostgresError())


def test_a_database_error_is_reduced_to_class_sqlstate_and_constraint(
    make_settings: SettingsFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(make_settings())
    try:
        raise a_database_error()
    except IntegrityError:
        structlog.get_logger().exception("http.error")
    (line,) = emitted(capsys)
    assert line["error"] == {
        "type": "FakePostgresError",
        "sqlstate": "23505",
        "constraint": "uq_users_email",
    }
    assert "exception" not in line
    assert "a@x.in" not in json.dumps(line)


def test_a_database_error_wrapped_in_another_exception_is_scrubbed_too(
    make_settings: SettingsFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(make_settings())
    try:
        try:
            raise a_database_error()
        except IntegrityError as error:
            raise RuntimeError("could not save") from error
    except RuntimeError:
        structlog.get_logger().exception("http.error")
    (line,) = emitted(capsys)
    assert line["error"]["constraint"] == "uq_users_email"
    assert "a@x.in" not in json.dumps(line)


def test_the_exception_object_form_is_scrubbed_too(
    make_settings: SettingsFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    """The catch-all handler logs `exc_info=exc`, not `exc_info=True`."""
    configure_logging(make_settings())
    structlog.get_logger().error("unhandled_exception", exc_info=a_database_error())
    (line,) = emitted(capsys)
    assert line["error"]["sqlstate"] == "23505"
    assert "a@x.in" not in json.dumps(line)


def test_a_line_break_in_a_value_cannot_forge_a_second_log_line(
    make_settings: SettingsFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(make_settings())
    structlog.get_logger().info("login", detail='x"}\n{"event": "forged"')
    assert len(capsys.readouterr().out.splitlines()) == 1


def test_the_console_format_works_and_prints_no_locals(
    make_settings: SettingsFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(make_settings(log_format="console"))
    try:
        fail_with_a_secret_in_a_local()
    except RuntimeError:
        structlog.get_logger().exception("http.error")
    output = capsys.readouterr().out
    assert "http.error" in output
    assert "correct-horse-battery-staple" not in output


def test_uvicorn_access_log_is_disabled_and_error_tracebacks_are_dropped(
    make_settings: SettingsFactory,
) -> None:
    configure_logging(make_settings())
    configure_logging(make_settings())  # a second start-up must not stack a second filter
    assert logging.getLogger("uvicorn.access").disabled is True
    error_logger = logging.getLogger("uvicorn.error")
    assert sum(isinstance(f, DropTraceback) for f in error_logger.filters) == 1

    try:
        raise RuntimeError("DETAIL: Key (email)=(a@x.in)")
    except RuntimeError:
        record = logging.LogRecord(
            "uvicorn.error", logging.ERROR, __file__, 1, "Exception in ASGI application", (), None
        )
        record.exc_info = sys.exc_info()
    assert error_logger.filter(record)
    assert record.exc_info is None
    assert record.getMessage() == "Exception in ASGI application"  # the line stays

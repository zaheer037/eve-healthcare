"""AB28: what a real `fastapi run` process prints, standard output and standard error alike.

The in-process tests see structlog events before they are rendered. Only a real process shows what
Uvicorn adds on its own: an access log with the query string, and the traceback of an unhandled
error with the `DETAIL` line of PostgreSQL, which echoes row values (spec sharp edge 42).
"""

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx2
import pytest
from sqlalchemy import Engine, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.security import hash_password
from app.models.user import Role
from tests.database import PROJECT_ROOT
from tests.factories import make_user

EMAIL = "audit-real-process@example.in"
PASSWORD = "a long enough password 123"
ADMIN_EMAIL = "admin-real-process@example.in"
ADMIN_PASSWORD = "an admin password of some length"
NEEDLE = "needle-in-the-query-string"
CENTRE_NAME = "Row Value Lab"
FORCED_CONSTRAINT = "ck_forced_for_test"

# A trigger that fails the way a broken constraint does. Its DETAIL echoes the row, as PostgreSQL
# does for a real violation, so the test proves that the row value never reaches the output.
FORCE_A_CONSTRAINT_ERROR = f"""
CREATE FUNCTION eve_force_constraint() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'forced for the test: %', NEW.email
    USING ERRCODE = '23514', CONSTRAINT = '{FORCED_CONSTRAINT}',
          DETAIL = 'Failing row contains (' || NEW.email || ', ' || NEW.password_hash || ')';
END $$;
CREATE TRIGGER eve_force_constraint BEFORE INSERT ON users
  FOR EACH ROW WHEN (NEW.email = '{EMAIL}') EXECUTE FUNCTION eve_force_constraint();
"""
FORCE_A_GENERIC_ERROR = f"""
CREATE FUNCTION eve_force_generic() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'boom for {{{{%}}}}', NEW.name;
END $$;
CREATE TRIGGER eve_force_generic BEFORE INSERT ON centres
  FOR EACH ROW WHEN (NEW.name = '{CENTRE_NAME}') EXECUTE FUNCTION eve_force_generic();
"""
DROP_THE_TRIGGERS = """
DROP TRIGGER IF EXISTS eve_force_constraint ON users;
DROP FUNCTION IF EXISTS eve_force_constraint();
DROP TRIGGER IF EXISTS eve_force_generic ON centres;
DROP FUNCTION IF EXISTS eve_force_generic();
"""


@dataclass
class RunningServer:
    base_url: str
    process: subprocess.Popen[bytes]
    stdout: Path
    stderr: Path

    def output(self) -> str:
        return self.stdout.read_text(errors="replace") + self.stderr.read_text(errors="replace")


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def wait_until_ready(base_url: str, process: subprocess.Popen[bytes]) -> None:
    for _ in range(100):
        if process.poll() is not None:
            raise RuntimeError("the server stopped before it was ready")
        try:
            if httpx2.get(f"{base_url}/health/ready", timeout=1).status_code == 200:
                return
        except httpx2.TransportError:
            time.sleep(0.2)
    raise RuntimeError("the server was not ready in time")


@pytest.fixture
def server(
    migrated_database: URL, test_settings: Settings, tmp_path: Path
) -> Iterator[RunningServer]:
    port = free_port()
    environment = {
        "PATH": os.environ["PATH"],
        "ENVIRONMENT": "test",
        "DATABASE_URL": migrated_database.render_as_string(hide_password=False),
        "JWT_SECRET": test_settings.jwt_secret.get_secret_value(),
        "WEBHOOK_SECRET": test_settings.webhook_secret.get_secret_value(),
        "LOG_FORMAT": "json",
        "PYTHONUNBUFFERED": "1",
    }
    stdout, stderr = tmp_path / "stdout.log", tmp_path / "stderr.log"
    with stdout.open("wb") as out, stderr.open("wb") as err:
        process = subprocess.Popen(  # noqa: S603  # a fixed argument list: this interpreter runs fastapi
            [sys.executable, "-m", "fastapi", "run", "--host", "127.0.0.1", "--port", str(port)],
            cwd=PROJECT_ROOT,
            env=environment,
            stdout=out,
            stderr=err,
        )
        base_url = f"http://127.0.0.1:{port}"
        try:
            wait_until_ready(base_url, process)
            yield RunningServer(base_url, process, stdout, stderr)
        finally:
            process.terminate()
            process.wait(timeout=15)


@pytest.fixture
def forced_failures(db_engine: Engine) -> Iterator[None]:
    with db_engine.begin() as connection:
        connection.execute(text(FORCE_A_CONSTRAINT_ERROR))
        connection.execute(text(FORCE_A_GENERIC_ERROR))
    try:
        yield
    finally:
        with db_engine.begin() as connection:
            connection.execute(text(DROP_THE_TRIGGERS))


def test_a_real_process_never_prints_a_row_value_a_secret_or_a_query_string(
    server: RunningServer, forced_failures: None, db_session: Session
) -> None:
    make_user(
        db_session, email=ADMIN_EMAIL, password_hash=hash_password(ADMIN_PASSWORD), role=Role.ADMIN
    )
    base = server.base_url
    with httpx2.Client(base_url=base, timeout=10) as client:
        # 1. A read with a query string: Uvicorn's own access log would print it.
        assert client.get("/centres/", params={"q": NEEDLE}).status_code == 200
        # 2. A constraint violation that the service does not map: the DETAIL echoes the row.
        constraint = client.post("/auth/signup", json={"email": EMAIL, "password": PASSWORD})
        assert constraint.status_code == 500
        assert constraint.json()["code"] == "INTERNAL_ERROR"
        # 3. An unhandled database exception whose message holds a row value.
        token = client.post(
            "/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
        ).json()["access_token"]
        generic = client.post(
            "/centres/",
            json={"name": CENTRE_NAME, "location": "1 Road"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert generic.status_code == 500
        assert generic.json()["code"] == "INTERNAL_ERROR"
        # The problem body itself holds no internal text either.
        for response in (constraint, generic):
            assert "forced for the test" not in response.text
            assert "boom" not in response.text

    server.process.terminate()
    server.process.wait(timeout=15)
    output = server.output()
    assert FORCED_CONSTRAINT in output  # the constraint name is what an operator needs
    assert "unhandled_exception" in output
    for forbidden in (
        EMAIL,
        PASSWORD,
        ADMIN_EMAIL,
        ADMIN_PASSWORD,
        token,
        NEEDLE,  # the query string
        CENTRE_NAME,  # a row value in the error message
        "$argon2",  # a password hash in the DETAIL line
        "Failing row contains",
        "forced for the test",
        "boom for",
    ):
        assert forbidden not in output, f"the output holds {forbidden!r}"

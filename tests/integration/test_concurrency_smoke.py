"""The concurrency helper works: N requests start together and all finish."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.support import run_concurrently


@pytest.mark.concurrency
def test_parallel_readiness_probes_all_succeed(app: FastAPI, client: TestClient) -> None:
    """One client per thread. The lifespan already ran through `client`, so the state exists."""
    calls = [lambda: TestClient(app).get("/health/ready").status_code for _ in range(10)]
    assert run_concurrently(calls) == [200] * 10

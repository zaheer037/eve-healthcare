"""The probes against a real database, and the query counter."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.support import QueryCounter


def test_readiness_answers_200_when_the_database_does(client: TestClient) -> None:
    response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_runs_exactly_one_statement(
    client: TestClient, query_counter: QueryCounter
) -> None:
    with query_counter:
        client.get("/health/ready")
    assert query_counter.count == 1
    query_counter.assert_at_most(1)


def test_the_counter_skips_the_statements_it_is_told_to_ignore(
    app: FastAPI, client: TestClient
) -> None:
    counter = QueryCounter(app.state.engine, ignore=("SELECT 1",))
    with counter:
        client.get("/health/ready")
    assert counter.count == 0


def test_the_counter_fails_a_test_over_budget(app: FastAPI, client: TestClient) -> None:
    counter = QueryCounter(app.state.engine)
    with counter:
        client.get("/health/ready")
    with pytest.raises(AssertionError, match="1 statements, budget 0"):
        counter.assert_at_most(0)


def test_the_connection_goes_back_to_the_pool_before_the_response_ends(
    app: FastAPI, client: TestClient
) -> None:
    client.get("/health/ready")
    assert app.state.engine.pool.checkedout() == 0

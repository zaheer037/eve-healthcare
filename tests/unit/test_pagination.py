"""The page parameters and the page envelope (AB18)."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.pagination import Page, PageParams, PageParamsDep, Pagination
from app.core.problem import install_exception_handlers


def build_app() -> FastAPI:
    app = FastAPI()
    install_exception_handlers(app)

    @app.get("/items")
    def items(page: PageParamsDep) -> dict[str, int]:
        return {"limit": page.limit, "offset": page.offset}

    return app


@pytest.fixture
def client() -> TestClient:
    return TestClient(build_app())


def test_the_defaults_are_20_and_0(client: TestClient) -> None:
    assert client.get("/items").json() == {"limit": 20, "offset": 0}


@pytest.mark.parametrize("query", ["limit=1&offset=0", "limit=100&offset=10000"])
def test_the_bounds_themselves_are_accepted(client: TestClient, query: str) -> None:
    assert client.get(f"/items?{query}").status_code == 200


@pytest.mark.parametrize(
    ("query", "field"),
    [
        ("limit=0", "query.limit"),
        ("limit=101", "query.limit"),
        ("limit=100000", "query.limit"),  # AB18
        ("limit=-5", "query.limit"),
        ("limit=abc", "query.limit"),
        ("offset=-1", "query.offset"),
        ("offset=10001", "query.offset"),
    ],
)
def test_values_outside_the_caps_are_a_422_that_names_the_field(
    client: TestClient, query: str, field: str
) -> None:
    response = client.get(f"/items?{query}")
    assert response.status_code == 422
    assert [item["field"] for item in response.json()["errors"]] == [field]


def test_params_describe_a_page_of_a_total() -> None:
    assert PageParams(limit=5, offset=10).describe(42) == Pagination(total=42, limit=5, offset=10)


def test_the_envelope_has_data_and_pagination() -> None:
    page = Page[int](data=[1, 2], pagination=PageParams().describe(2))
    assert page.model_dump() == {
        "data": [1, 2],
        "pagination": {"total": 2, "limit": 20, "offset": 0},
    }

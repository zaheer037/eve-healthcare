"""The catalog over HTTP: public reads, admin writes, filters, paging and soft delete.

Covers AB2, AB15, AB17, AB18 and AB33 of the architecture document (§7.4).
"""

import uuid
from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.catalog import Centre, CentreTest, DiagnosticTest
from tests.factories import make_centre, make_centre_test, make_diagnostic_test
from tests.support import AUTH_LOOKUP, QueryCounter, run_concurrently

Headers = dict[str, str]


def post_centre(client: TestClient, headers: Headers | None = None, **overrides: Any) -> Response:
    body = {"name": f"Lab {uuid.uuid4().hex[:8]}", "location": "12 MG Road", "city": "Pune"}
    return client.post("/centres/", json={**body, **overrides}, headers=headers)


def post_test(client: TestClient, headers: Headers | None = None, **overrides: Any) -> Response:
    suffix = uuid.uuid4().hex[:8]
    body = {"code": f"T{suffix.upper()}", "name": f"Test {suffix}"}
    return client.post("/tests/", json={**body, **overrides}, headers=headers)


def put_price(
    client: TestClient,
    centre_id: object,
    test_id: object,
    headers: Headers | None = None,
    **body: Any,
) -> Response:
    return client.put(f"/centres/{centre_id}/tests/{test_id}", json=body, headers=headers)


def count(
    db_session: Session, model: type[Centre] | type[DiagnosticTest] | type[CentreTest]
) -> int:
    return db_session.scalar(select(func.count()).select_from(model)) or 0


# ==== public reads


def test_anyone_can_list_the_active_centres_without_a_token(
    client: TestClient, db_session: Session
) -> None:
    make_centre(db_session, name="Zeta Lab")
    make_centre(db_session, name="Alpha Lab")
    make_centre(db_session, name="Hidden Lab", is_active=False)
    response = client.get("/centres/")
    body = response.json()
    assert response.status_code == 200
    assert [item["name"] for item in body["data"]] == ["Alpha Lab", "Zeta Lab"]
    assert body["pagination"] == {"total": 2, "limit": 20, "offset": 0}
    assert set(body["data"][0]) == {"id", "name", "location", "city", "is_active", "price"}
    assert body["data"][0]["price"] is None


def test_a_centre_detail_lists_the_tests_that_it_offers(
    client: TestClient, db_session: Session
) -> None:
    centre = make_centre(db_session, city=None)
    cbc = make_diagnostic_test(db_session, code="CBC", name="Complete Blood Count")
    make_centre_test(db_session, centre=centre, test=cbc, price=Decimal("499"))
    make_centre_test(
        db_session, centre=centre, test=make_diagnostic_test(db_session), is_available=False
    )
    response = client.get(f"/centres/{centre.id}")
    body = response.json()
    assert response.status_code == 200
    assert body["city"] is None
    assert body["tests"] == [
        {
            "test_id": str(cbc.id),
            "code": "CBC",
            "name": "Complete Blood Count",
            "price": "499.00",  # money is a decimal string
            "currency": "INR",
            "is_available": True,
        }
    ]


def test_the_filters_work_over_http(client: TestClient, db_session: Session) -> None:
    test = make_diagnostic_test(db_session)
    pune = make_centre(db_session, name="Sunrise Pune", city="Pune")
    make_centre_test(db_session, centre=pune, test=test, price=Decimal("250"))
    make_centre(db_session, name="Sunrise Delhi", city="Delhi")
    by_city = client.get("/centres/", params={"city": "PUNE"}).json()
    assert [item["name"] for item in by_city["data"]] == ["Sunrise Pune"]
    by_test = client.get("/centres/", params={"test_id": str(test.id)}).json()
    assert [(item["name"], item["price"]) for item in by_test["data"]] == [
        ("Sunrise Pune", "250.00")
    ]
    by_text = client.get("/centres/", params={"q": "sunrise"}).json()
    assert by_text["pagination"]["total"] == 2
    combined = client.get("/centres/", params={"q": "sun", "city": "delhi"}).json()
    assert [item["name"] for item in combined["data"]] == ["Sunrise Delhi"]


def test_paging_reports_the_total_and_echoes_limit_and_offset(
    client: TestClient, db_session: Session
) -> None:
    for number in range(7):
        make_centre(db_session, name=f"Lab {number}")
    body = client.get("/centres/", params={"limit": 3, "offset": 6}).json()
    assert [item["name"] for item in body["data"]] == ["Lab 6"]
    assert body["pagination"] == {"total": 7, "limit": 3, "offset": 6}


@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({"limit": 0}, "query.limit"),
        ({"limit": 101}, "query.limit"),
        ({"limit": 100000}, "query.limit"),  # AB18
        ({"offset": -1}, "query.offset"),
        ({"offset": 10001}, "query.offset"),
        ({"test_id": "not-a-uuid"}, "query.test_id"),
        ({"q": ""}, "query.q"),
        ({"q": "x" * 101}, "query.q"),
        ({"city": "x" * 81}, "query.city"),
        ({"q": "a\x00b"}, "query.q"),
    ],
)
def test_bad_query_parameters_are_a_422_that_names_the_field(
    client: TestClient, params: dict[str, Any], field: str
) -> None:
    response = client.get("/centres/", params=params)
    assert response.status_code == 422
    assert [item["field"] for item in response.json()["errors"]] == [field]


@pytest.mark.parametrize("params", [{"limit": 100, "offset": 10000}, {"limit": 1}])
def test_the_paging_bounds_themselves_are_accepted(
    client: TestClient, params: dict[str, int]
) -> None:
    assert client.get("/centres/", params=params).status_code == 200


def test_hostile_text_in_the_search_filter_is_harmless(
    client: TestClient, db_session: Session
) -> None:
    """AB17."""
    make_centre(db_session, name="A_B Lab")
    make_centre(db_session, name="AxB Lab")
    for hostile in ("'; DROP TABLE centres; --", "%", "\\", "' OR '1'='1"):
        assert client.get("/centres/", params={"q": hostile}).status_code == 200
    literal = client.get("/centres/", params={"q": "A_B"}).json()
    assert [item["name"] for item in literal["data"]] == ["A_B Lab"]
    assert client.get("/centres/").json()["pagination"]["total"] == 2  # the table is intact


def test_a_malformed_centre_id_is_422_and_an_unknown_or_inactive_one_is_404(
    client: TestClient, db_session: Session
) -> None:
    """AB15."""
    assert client.get("/centres/not-a-uuid").status_code == 422
    unknown = client.get(f"/centres/{uuid.uuid4()}")
    assert unknown.status_code == 404
    assert unknown.json()["code"] == "CENTRE_NOT_FOUND"
    inactive = make_centre(db_session, is_active=False)
    assert client.get(f"/centres/{inactive.id}").status_code == 404


def test_the_tests_can_be_listed_searched_and_read_without_a_token(
    client: TestClient, db_session: Session
) -> None:
    make_diagnostic_test(
        db_session, code="HBA1C", name="Glycated haemoglobin", description="Sugar."
    )
    make_diagnostic_test(db_session, code="TSH", name="Thyroid stimulating hormone")
    make_diagnostic_test(db_session, code="HIDDEN", name="Hidden", is_active=False)
    everything = client.get("/tests/").json()
    assert [item["code"] for item in everything["data"]] == ["HBA1C", "TSH"]
    found = client.get("/tests/", params={"q": "hba"}).json()
    assert [item["code"] for item in found["data"]] == ["HBA1C"]
    one = client.get(f"/tests/{found['data'][0]['id']}").json()
    assert set(one) == {"id", "code", "name", "description", "is_active"}
    assert one["description"] == "Sugar."
    assert client.get(f"/tests/{uuid.uuid4()}").json()["code"] == "DIAGNOSTIC_TEST_NOT_FOUND"
    assert client.get("/tests/not-a-uuid").status_code == 422


# ==== who may write (AB2)


WRITES = [
    ("post", "/centres/", {"name": "x", "location": "y"}),
    ("patch", f"/centres/{uuid.uuid4()}", {"name": "x"}),
    ("delete", f"/centres/{uuid.uuid4()}", None),
    ("post", "/tests/", {"code": "ABC", "name": "x"}),
    ("patch", f"/tests/{uuid.uuid4()}", {"name": "x"}),
    ("delete", f"/tests/{uuid.uuid4()}", None),
    ("put", f"/centres/{uuid.uuid4()}/tests/{uuid.uuid4()}", {"price": "10.00"}),
]


@pytest.mark.parametrize(("method", "path", "body"), WRITES)
def test_a_patient_cannot_write_the_catalog(
    client: TestClient, patient_headers: Headers, method: str, path: str, body: Any
) -> None:
    response = client.request(method, path, json=body, headers=patient_headers)
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"


def test_a_patient_write_creates_no_row(
    client: TestClient, patient_headers: Headers, db_session: Session
) -> None:
    """AB2."""
    assert post_centre(client, patient_headers).status_code == 403
    assert post_test(client, patient_headers).status_code == 403
    assert count(db_session, Centre) == 0
    assert count(db_session, DiagnosticTest) == 0


@pytest.mark.parametrize(("method", "path", "body"), WRITES)
def test_a_write_without_a_token_is_401(
    client: TestClient, method: str, path: str, body: Any
) -> None:
    response = client.request(method, path, json=body)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_the_admin_check_comes_before_the_body_is_validated(client: TestClient) -> None:
    """A well-formed body with invalid fields and no token gets 401, not 422 (architecture §2.4)."""
    assert client.post("/centres/", json={"name": ""}).status_code == 401


# ==== admin: centres


def test_an_admin_creates_a_centre_and_the_location_points_at_it(
    client: TestClient, admin_headers: Headers
) -> None:
    response = post_centre(client, admin_headers, name="City Lab", city=None)
    body = response.json()
    assert response.status_code == 201
    assert (body["name"], body["city"], body["is_active"], body["price"]) == (
        "City Lab",
        None,
        True,
        None,
    )
    assert response.headers["location"] == f"/centres/{body['id']}"
    assert client.get(response.headers["location"]).json()["name"] == "City Lab"


def test_the_location_header_is_a_path_and_ignores_a_hostile_host_header(
    client: TestClient, admin_headers: Headers
) -> None:
    response = client.post(
        "/centres/",
        json={"name": "Host Lab", "location": "1 Road"},
        headers={**admin_headers, "Host": "evil.example"},
    )
    assert response.headers["location"].startswith("/centres/")
    assert "evil.example" not in response.headers["location"]


def test_a_duplicate_centre_is_409(client: TestClient, admin_headers: Headers) -> None:
    assert post_centre(client, admin_headers, name="Twin", location="1 Road").status_code == 201
    response = post_centre(client, admin_headers, name="Twin", location="1 Road")
    assert response.status_code == 409
    assert response.json()["code"] == "DUPLICATE_CENTRE"


@pytest.mark.parametrize(
    "body",
    [
        {"name": "", "location": "l"},
        {"name": "n"},
        {"name": "n", "location": "l", "is_active": False},
        {"name": "n", "location": "l", "id": str(uuid.uuid4())},
        {"name": "n\x00", "location": "l"},
        {"name": "n", "location": "l", "city": "x" * 81},
    ],
)
def test_an_invalid_centre_is_a_422_and_creates_no_row(
    client: TestClient, admin_headers: Headers, db_session: Session, body: dict[str, Any]
) -> None:
    assert client.post("/centres/", json=body, headers=admin_headers).status_code == 422
    assert count(db_session, Centre) == 0


def test_patch_changes_only_the_fields_that_were_sent(
    client: TestClient, admin_headers: Headers, db_session: Session
) -> None:
    centre = make_centre(db_session, name="Old", location="1 Road", city="Pune")
    response = client.patch(f"/centres/{centre.id}", json={"name": "New"}, headers=admin_headers)
    assert response.status_code == 200
    assert (response.json()["name"], response.json()["location"], response.json()["city"]) == (
        "New",
        "1 Road",
        "Pune",
    )
    cleared = client.patch(f"/centres/{centre.id}", json={"city": None}, headers=admin_headers)
    assert cleared.json()["city"] is None


@pytest.mark.parametrize(
    "body",
    [{"name": None}, {"location": None}, {"is_active": None}, {}, {"role": "x"}, {"name": ""}],
)
def test_a_patch_with_null_an_empty_body_or_an_unknown_field_is_422(
    client: TestClient, admin_headers: Headers, db_session: Session, body: dict[str, Any]
) -> None:
    """AB33: never a 500."""
    centre = make_centre(db_session, name="Stays")
    response = client.patch(f"/centres/{centre.id}", json=body, headers=admin_headers)
    assert response.status_code == 422
    db_session.refresh(centre)
    assert centre.name == "Stays"


def test_patch_to_an_existing_name_and_location_is_409(
    client: TestClient, admin_headers: Headers, db_session: Session
) -> None:
    make_centre(db_session, name="Taken", location="1 Road")
    other = make_centre(db_session, name="Free", location="1 Road")
    response = client.patch(f"/centres/{other.id}", json={"name": "Taken"}, headers=admin_headers)
    assert response.status_code == 409


def test_patch_an_unknown_centre_is_404_and_a_malformed_id_is_422(
    client: TestClient, admin_headers: Headers
) -> None:
    assert (
        client.patch(
            f"/centres/{uuid.uuid4()}", json={"name": "x"}, headers=admin_headers
        ).status_code
        == 404
    )
    assert (
        client.patch("/centres/nope", json={"name": "x"}, headers=admin_headers).status_code == 422
    )


def test_delete_deactivates_hides_and_can_be_repeated_and_patch_restores(
    client: TestClient, admin_headers: Headers, db_session: Session
) -> None:
    centre = make_centre(db_session, name="Sleepy Lab")
    first = client.delete(f"/centres/{centre.id}", headers=admin_headers)
    assert first.status_code == 204
    assert first.content == b""
    assert (
        client.delete(f"/centres/{centre.id}", headers=admin_headers).status_code == 204
    )  # repeated
    assert client.get(f"/centres/{centre.id}").status_code == 404
    assert client.get("/centres/").json()["pagination"]["total"] == 0
    assert count(db_session, Centre) == 1  # a centre is never deleted
    restored = client.patch(
        f"/centres/{centre.id}", json={"is_active": True}, headers=admin_headers
    )
    assert restored.status_code == 200
    assert client.get(f"/centres/{centre.id}").status_code == 200


def test_delete_an_unknown_centre_is_404(client: TestClient, admin_headers: Headers) -> None:
    assert client.delete(f"/centres/{uuid.uuid4()}", headers=admin_headers).status_code == 404


# ==== admin: diagnostic tests


def test_an_admin_creates_reads_changes_and_deactivates_a_test(
    client: TestClient, admin_headers: Headers
) -> None:
    created = post_test(
        client, admin_headers, code="CBC", name="Complete Blood Count", description="Blood."
    )
    body = created.json()
    assert created.status_code == 201
    assert created.headers["location"] == f"/tests/{body['id']}"
    assert (body["code"], body["description"], body["is_active"]) == ("CBC", "Blood.", True)
    url = f"/tests/{body['id']}"
    assert (
        client.patch(url, json={"description": None}, headers=admin_headers).json()["description"]
        is None
    )
    assert client.delete(url, headers=admin_headers).status_code == 204
    assert client.get(url).status_code == 404
    assert client.patch(url, json={"is_active": True}, headers=admin_headers).status_code == 200
    assert client.get(url).status_code == 200


def test_a_duplicate_test_code_or_name_is_409(client: TestClient, admin_headers: Headers) -> None:
    assert (
        post_test(client, admin_headers, code="CBC", name="Complete Blood Count").status_code == 201
    )
    for clash in (
        {"code": "CBC", "name": "Other"},
        {"code": "OTHER", "name": "Complete Blood Count"},
    ):
        response = post_test(client, admin_headers, **clash)
        assert response.status_code == 409
        assert response.json()["code"] == "DUPLICATE_DIAGNOSTIC_TEST"


@pytest.mark.parametrize(
    "body",
    [
        {"code": "cbc", "name": "n"},
        {"code": "A", "name": "n"},
        {"code": "CBC\n", "name": "n"},
        {"code": "CBC"},
        {"code": "CBC", "name": "n", "is_active": False},
    ],
)
def test_an_invalid_test_is_a_422(
    client: TestClient, admin_headers: Headers, body: dict[str, Any]
) -> None:
    assert client.post("/tests/", json=body, headers=admin_headers).status_code == 422


def test_a_test_patch_with_null_on_a_required_field_is_422(
    client: TestClient, admin_headers: Headers, db_session: Session
) -> None:
    test = make_diagnostic_test(db_session, code="KEEP")
    for body in ({"code": None}, {"name": None}, {}):
        assert (
            client.patch(f"/tests/{test.id}", json=body, headers=admin_headers).status_code == 422
        )


# ==== admin: the price of a test at a centre (T2.4)


def test_the_first_put_is_201_and_the_second_is_200(
    client: TestClient, admin_headers: Headers, db_session: Session
) -> None:
    centre = make_centre(db_session)
    test = make_diagnostic_test(db_session, code="CBC", name="Complete Blood Count")
    created = put_price(client, centre.id, test.id, admin_headers, price="499")
    assert created.status_code == 201
    assert created.json() == {
        "test_id": str(test.id),
        "code": "CBC",
        "name": "Complete Blood Count",
        "price": "499.00",
        "currency": "INR",
        "is_available": True,
    }
    updated = put_price(client, centre.id, test.id, admin_headers, price="550.5", currency="USD")
    assert updated.status_code == 200
    assert (updated.json()["price"], updated.json()["currency"]) == ("550.50", "USD")
    assert count(db_session, CentreTest) == 1


def test_repeating_a_put_gives_the_same_answer(
    client: TestClient, admin_headers: Headers, db_session: Session
) -> None:
    centre, test = make_centre(db_session), make_diagnostic_test(db_session)
    put_price(client, centre.id, test.id, admin_headers, price="120", is_available=False)
    again = [
        put_price(client, centre.id, test.id, admin_headers, price="120", is_available=False)
        for _ in range(2)
    ]
    assert [response.status_code for response in again] == [200, 200]
    assert again[0].json() == again[1].json()
    assert count(db_session, CentreTest) == 1


def test_a_put_takes_the_currency_and_the_availability_from_their_defaults(
    client: TestClient, admin_headers: Headers, db_session: Session
) -> None:
    centre, test = make_centre(db_session), make_diagnostic_test(db_session)
    body = put_price(client, centre.id, test.id, admin_headers, price="80").json()
    assert (body["currency"], body["is_available"]) == ("INR", True)


@pytest.mark.parametrize("price", ["0.01", "99999999.99", "1", 1.5, 250])
def test_the_price_bounds_themselves_are_accepted(
    client: TestClient, admin_headers: Headers, db_session: Session, price: Any
) -> None:
    centre, test = make_centre(db_session), make_diagnostic_test(db_session)
    assert put_price(client, centre.id, test.id, admin_headers, price=price).status_code == 201


def test_a_price_shows_in_the_detail_and_in_the_filtered_list_until_it_is_withdrawn(
    client: TestClient, admin_headers: Headers, db_session: Session
) -> None:
    centre = make_centre(db_session, name="Priced Lab")
    test = make_diagnostic_test(db_session, code="LFT", name="Liver Function Test")
    put_price(client, centre.id, test.id, admin_headers, price="650")
    detail = client.get(f"/centres/{centre.id}").json()
    assert [(item["code"], item["price"]) for item in detail["tests"]] == [("LFT", "650.00")]
    listed = client.get("/centres/", params={"test_id": str(test.id)}).json()
    assert [(item["name"], item["price"]) for item in listed["data"]] == [("Priced Lab", "650.00")]
    put_price(client, centre.id, test.id, admin_headers, price="650", is_available=False)
    assert client.get(f"/centres/{centre.id}").json()["tests"] == []
    assert client.get("/centres/", params={"test_id": str(test.id)}).json()["data"] == []


def test_a_put_for_an_unknown_centre_or_test_is_404_with_its_own_code(
    client: TestClient, admin_headers: Headers, db_session: Session
) -> None:
    centre, test = make_centre(db_session), make_diagnostic_test(db_session)
    no_centre = put_price(client, uuid.uuid4(), test.id, admin_headers, price="10")
    no_test = put_price(client, centre.id, uuid.uuid4(), admin_headers, price="10")
    assert (no_centre.status_code, no_centre.json()["code"]) == (404, "CENTRE_NOT_FOUND")
    assert (no_test.status_code, no_test.json()["code"]) == (404, "DIAGNOSTIC_TEST_NOT_FOUND")
    assert count(db_session, CentreTest) == 0


def test_a_put_with_a_malformed_id_is_422(client: TestClient, admin_headers: Headers) -> None:
    good = uuid.uuid4()
    for centre_id, test_id in (("nope", good), (good, "nope")):
        response = put_price(client, centre_id, test_id, admin_headers, price="10")
        assert response.status_code == 422


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"price": None},
        {"price": 0},
        {"price": "0.00"},
        {"price": "0.009"},
        {"price": -5},
        {"price": "100000000"},
        {"price": "99999999.999"},
        {"price": "10.005"},
        {"price": "NaN"},
        {"price": "Infinity"},
        {"price": "abc"},
        {"price": 10, "currency": "inr"},
        {"price": 10, "currency": "INRR"},
        {"price": 10, "currency": ""},
        {"price": 10, "is_available": "maybe"},
        {"price": 10, "unknown": 1},
    ],
)
def test_an_invalid_price_body_is_a_422_and_stores_nothing(
    client: TestClient,
    admin_headers: Headers,
    db_session: Session,
    body: dict[str, Any],
) -> None:
    centre, test = make_centre(db_session), make_diagnostic_test(db_session)
    response = put_price(client, centre.id, test.id, admin_headers, **body)
    assert response.status_code == 422
    assert count(db_session, CentreTest) == 0


@pytest.mark.concurrency
def test_a_race_of_puts_creates_the_price_once(
    client: TestClient, admin_headers: Headers, db_session: Session
) -> None:
    """Exactly one of the racing calls inserts the row and answers 201. The rest update it."""
    centre, test = make_centre(db_session), make_diagnostic_test(db_session)
    calls = [
        lambda price=price: (
            put_price(  # type: ignore[misc]
                client, centre.id, test.id, admin_headers, price=str(price)
            ).status_code
        )
        for price in range(100, 108)
    ]
    assert sorted(run_concurrently(calls)) == [200] * 7 + [201]
    assert count(db_session, CentreTest) == 1


# ==== statement budgets (architecture §5.2)


def test_the_statement_budgets_hold_over_http(
    app: FastAPI, client: TestClient, admin_headers: Headers, db_session: Session
) -> None:
    centre = make_centre(db_session)
    test, other = make_diagnostic_test(db_session), make_diagnostic_test(db_session)
    make_centre_test(db_session, centre=centre, test=test)
    with QueryCounter(app.state.engine, ignore=(AUTH_LOOKUP,)) as counter:
        checks = [
            ("GET", "/centres/", None, 2),
            ("GET", f"/centres/{centre.id}", None, 2),
            ("GET", "/tests/", None, 2),
            ("GET", f"/tests/{test.id}", None, 1),
            ("POST", "/centres/", {"name": "Budget Lab", "location": "1 Road"}, 1),
            ("PATCH", f"/centres/{centre.id}", {"name": "Renamed"}, 2),
            ("PUT", f"/centres/{centre.id}/tests/{other.id}", {"price": "10.00"}, 1),
            ("PUT", f"/centres/{centre.id}/tests/{other.id}", {"price": "20.00"}, 1),
            ("DELETE", f"/centres/{centre.id}", None, 1),
        ]
        for method, path, body, budget in checks:
            counter.statements.clear()
            response = client.request(method, path, json=body, headers=admin_headers)
            assert response.status_code < 300, (method, path, response.text)
            assert counter.count <= budget, (method, path, counter.count, counter.statements)

"""The OpenAPI document and the HTTP conventions, checked against architecture §§5.1 and 5.2.

The inventory below is table 5.2 of the architecture document. When an endpoint changes, this
table changes with it, and the documentation cannot drift from the code without a test failing.
"""

import re
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

SHARED = {400, 413, 500, 503}  # every operation can answer these (architecture §5.2)


def expect(*success: int, errors: tuple[int, ...] = ()) -> tuple[set[int], set[int]]:
    return set(success), set(errors)


# (method, path) -> (success statuses, error statuses that architecture §5.2 lists)
INVENTORY: dict[tuple[str, str], tuple[set[int], set[int]]] = {
    ("GET", "/health/live"): expect(200),
    ("GET", "/health/ready"): expect(200),
    ("POST", "/auth/signup"): expect(201, errors=(409, 422, 429)),
    ("POST", "/auth/login"): expect(200, errors=(401, 422, 429)),
    ("POST", "/auth/token"): expect(200, errors=(401, 422, 429)),
    ("GET", "/users/me"): expect(200, errors=(401,)),
    ("GET", "/centres/"): expect(200, errors=(422,)),
    ("POST", "/centres/"): expect(201, errors=(401, 403, 409, 422)),
    ("GET", "/centres/{centre_id}"): expect(200, errors=(404, 422)),
    ("PATCH", "/centres/{centre_id}"): expect(200, errors=(401, 403, 404, 409, 422)),
    ("DELETE", "/centres/{centre_id}"): expect(204, errors=(401, 403, 404, 422)),
    ("PUT", "/centres/{centre_id}/tests/{test_id}"): expect(200, 201, errors=(401, 403, 404, 422)),
    ("GET", "/tests/"): expect(200, errors=(422,)),
    ("POST", "/tests/"): expect(201, errors=(401, 403, 409, 422)),
    ("GET", "/tests/{test_id}"): expect(200, errors=(404, 422)),
    ("PATCH", "/tests/{test_id}"): expect(200, errors=(401, 403, 404, 409, 422)),
    ("DELETE", "/tests/{test_id}"): expect(204, errors=(401, 403, 404, 422)),
    ("POST", "/bookings/"): expect(201, errors=(401, 404, 409, 422)),
    ("GET", "/bookings/"): expect(200, errors=(401, 422)),
    ("GET", "/bookings/{booking_id}"): expect(200, errors=(401, 403, 404, 422)),
    ("POST", "/bookings/{booking_id}/cancel"): expect(200, errors=(401, 403, 404, 409, 422)),
    ("POST", "/payments/"): expect(200, 201, 202, errors=(401, 403, 404, 409, 422)),
    ("GET", "/payments/{payment_id}"): expect(200, errors=(401, 403, 404, 422)),
    ("POST", "/payments/webhook/"): expect(200, errors=(401, 413, 422)),
}


@pytest.fixture
def schema(client: TestClient) -> dict[str, Any]:
    response = client.get("/openapi.json")
    assert response.status_code == 200
    document: dict[str, Any] = response.json()
    return document


def operations(schema: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (method.upper(), path): operation
        for path, methods in schema["paths"].items()
        for method, operation in methods.items()
    }


def statuses(operation: dict[str, Any]) -> set[int]:
    return {int(status) for status in operation["responses"]}


# ==== the inventory


def test_the_documented_operations_are_exactly_the_24_endpoints(
    schema: dict[str, Any],
) -> None:
    assert set(operations(schema)) == set(INVENTORY)
    assert len(INVENTORY) == 24


def test_every_operation_has_a_summary_and_a_unique_stable_operation_id(
    schema: dict[str, Any],
) -> None:
    found = operations(schema)
    ids = [operation["operationId"] for operation in found.values()]
    assert len(ids) == len(set(ids))
    for (method, path), operation in found.items():
        assert operation.get("summary"), (method, path)
        # A readable id from the function name, not one built from the path and the method.
        assert re.fullmatch(r"[a-z][a-z0-9_]*", operation["operationId"]), operation["operationId"]
        assert not operation["operationId"].endswith(("_get", "_post", "_put", "_patch", "_delete"))


def unresolved_references(schema: dict[str, Any]) -> list[str]:
    """Every `$ref` in the document that points at nothing in the document."""

    def resolves(pointer: str) -> bool:
        node: Any = schema
        for part in pointer.removeprefix("#/").split("/"):
            if not isinstance(node, dict) or part not in node:
                return False
            node = node[part]
        return True

    missing: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            reference = node.get("$ref")
            if isinstance(reference, str) and not resolves(reference):
                missing.append(reference)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(schema)
    return missing


def test_every_reference_in_the_document_resolves(schema: dict[str, Any]) -> None:
    """A dangling `$ref` breaks Swagger UI and every client generator."""
    assert unresolved_references(schema) == []


def test_the_webhook_body_is_documented_through_the_components(schema: dict[str, Any]) -> None:
    """The route declares no body, so its schema is registered by hand, with the models under it."""
    operation = schema["paths"]["/payments/webhook/"]["post"]
    body = operation["requestBody"]["content"]["application/json"]["schema"]
    assert body == {"$ref": "#/components/schemas/WebhookPayload"}
    assert "WebhookPayload" in schema["components"]["schemas"]


def test_every_operation_lists_its_success_statuses_its_errors_and_the_shared_four(
    schema: dict[str, Any],
) -> None:
    for (method, path), operation in operations(schema).items():
        success, errors = INVENTORY[method, path]
        assert statuses(operation) == success | errors | SHARED, (method, path)


def test_every_error_response_is_problem_json_with_the_problem_model(
    schema: dict[str, Any],
) -> None:
    for (method, path), operation in operations(schema).items():
        for status, response in operation["responses"].items():
            if int(status) < 400:
                continue
            content = response["content"]
            assert set(content) == {"application/problem+json"}, (method, path, status)
            assert content["application/problem+json"]["schema"] == {
                "$ref": "#/components/schemas/ProblemDetail"
            }, (method, path, status)
            assert response["description"], (method, path, status)


def test_an_error_response_names_the_codes_that_cause_it(schema: dict[str, Any]) -> None:
    create = operations(schema)["POST", "/bookings/"]["responses"]
    assert "CENTRE_TEST_NOT_FOUND" in create["404"]["description"]
    assert "DUPLICATE_BOOKING" in create["409"]["description"]
    assert "BOOKING_LIMIT_REACHED" in create["409"]["description"]
    assert "NOT_AUTHENTICATED" in create["401"]["description"]
    assert (
        "PAYMENT_IN_PROGRESS"
        in operations(schema)["POST", "/bookings/{booking_id}/cancel"]["responses"]["409"][
            "description"
        ]
    )


def test_the_problem_model_lists_every_error_code(schema: dict[str, Any]) -> None:
    code = schema["components"]["schemas"]["ProblemDetail"]["properties"]["code"]
    codes = schema["components"]["schemas"][code["$ref"].rsplit("/", 1)[1]]["enum"]
    assert {"BOOKING_NOT_PAYABLE", "WEBHOOK_SIGNATURE_INVALID", "RATE_LIMITED"} <= set(codes)


def test_the_default_validation_error_model_is_gone(schema: dict[str, Any]) -> None:
    """Our 422 is problem JSON with an `errors` list, not FastAPI's default model."""
    assert "HTTPValidationError" not in schema["components"]["schemas"]


# ==== examples and descriptions


def test_every_json_body_has_an_example(schema: dict[str, Any]) -> None:
    for (method, path), operation in operations(schema).items():
        body = operation.get("requestBody", {}).get("content", {}).get("application/json")
        if body is None:
            continue
        if "example" in body or "examples" in body:
            continue
        reference = body["schema"]["$ref"].rsplit("/", 1)[1]
        assert schema["components"]["schemas"][reference].get("examples"), (method, path, reference)


def test_every_operation_has_a_description(schema: dict[str, Any]) -> None:
    for (method, path), operation in operations(schema).items():
        assert operation.get("description") or operation.get("summary"), (method, path)


def test_the_tags_are_described_and_the_bearer_scheme_is_offered(schema: dict[str, Any]) -> None:
    tags = {tag["name"]: tag.get("description") for tag in schema["tags"]}
    assert {"auth", "users", "centres", "tests", "bookings", "payments", "health"} <= set(tags)
    assert all(tags.values())
    scheme = schema["components"]["securitySchemes"]["OAuth2PasswordBearer"]
    assert scheme["flows"]["password"]["tokenUrl"] == "/auth/token"


def test_the_login_and_the_webhook_document_their_special_inputs(schema: dict[str, Any]) -> None:
    token = operations(schema)["POST", "/auth/token"]["requestBody"]["content"]
    assert "application/x-www-form-urlencoded" in token
    webhook = operations(schema)["POST", "/payments/webhook/"]
    assert any(p["name"] == "X-Webhook-Signature" for p in webhook["parameters"])


# ==== HTTP conventions that hold for every route (architecture §5.1)


def sample(path: str) -> str:
    return re.sub(r"\{[a-z_]+\}", lambda _: str(uuid.uuid4()), path)


def get_paths() -> list[str]:
    return sorted(path for method, path in INVENTORY if method == "GET")


@pytest.mark.parametrize("path", get_paths())
def test_head_works_wherever_get_does(client: TestClient, path: str) -> None:
    """AB34: RFC 9110 section 9.1."""
    url = sample(path)
    get, head = client.get(url), client.head(url)
    assert head.status_code == get.status_code
    assert head.headers["content-type"] == get.headers["content-type"]
    assert head.content == b""


@pytest.mark.parametrize(("method", "path"), sorted(INVENTORY))
def test_both_slash_spellings_answer_alike_and_never_redirect(
    client: TestClient, method: str, path: str
) -> None:
    url = sample(path)
    other = url.rstrip("/") if url.endswith("/") else url + "/"
    first = client.request(method, url, follow_redirects=False)
    second = client.request(method, other, follow_redirects=False)
    assert first.status_code == second.status_code
    assert not 300 <= first.status_code < 400
    assert "location" not in first.headers
    assert "location" not in second.headers


@pytest.mark.parametrize(
    ("method", "path", "status"),
    [
        ("GET", "/health/live", 200),
        ("GET", "/users/me", 401),
        ("GET", f"/centres/{uuid.UUID(int=1)}", 404),
        ("GET", "/centres/not-a-uuid", 422),
        ("PUT", "/bookings/", 405),
        ("GET", "/no/such/route", 404),
    ],
)
def test_every_response_says_no_store(
    client: TestClient, method: str, path: str, status: int
) -> None:
    response = client.request(method, path)
    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"


def test_a_405_lists_every_method_that_the_path_serves(client: TestClient) -> None:
    for path, served in (
        ("/bookings/", {"GET", "HEAD", "POST"}),
        ("/centres/", {"GET", "HEAD", "POST"}),
        (f"/bookings/{uuid.uuid4()}", {"GET", "HEAD"}),
        (f"/centres/{uuid.uuid4()}", {"GET", "HEAD", "PATCH", "DELETE"}),
    ):
        response = client.request("PUT", path)
        assert response.status_code == 405
        assert response.json()["code"] == "METHOD_NOT_ALLOWED"
        assert {method.strip() for method in response.headers["allow"].split(",")} == served


def test_the_hidden_second_spelling_is_not_in_the_document(schema: dict[str, Any]) -> None:
    paths = set(schema["paths"])
    assert "/centres" not in paths
    assert "/payments/webhook" not in paths

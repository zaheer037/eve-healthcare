"""Catalog input: bounds that match the database checks, so a client never reaches a CHECK."""

from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from app.catalog.schemas import (
    CentreCreate,
    CentreTestWrite,
    CentreUpdate,
    DiagnosticTestCreate,
    DiagnosticTestUpdate,
)


def refused(model: Any, payload: dict[str, Any]) -> set[str]:
    with pytest.raises(ValidationError) as error:
        model.model_validate(payload)
    return {str(item["loc"][0]) if item["loc"] else "" for item in error.value.errors()}


# ---------------------------------------------------------------- centres


def test_a_centre_needs_a_name_and_a_location_and_no_city() -> None:
    centre = CentreCreate.model_validate({"name": " City Lab ", "location": "12 MG Road"})
    assert (centre.name, centre.location, centre.city) == ("City Lab", "12 MG Road", None)


@pytest.mark.parametrize(("field", "limit"), [("name", 120), ("location", 200), ("city", 80)])
def test_centre_text_has_a_length_limit(field: str, limit: int) -> None:
    base = {"name": "n", "location": "l"}
    assert CentreCreate.model_validate({**base, field: "x" * limit})
    assert refused(CentreCreate, {**base, field: "x" * (limit + 1)}) == {field}


@pytest.mark.parametrize("field", ["name", "location", "city"])
@pytest.mark.parametrize("bad", ["", "   ", "a\x00b", "a\nb"])
def test_centre_text_is_not_empty_and_has_no_control_character(field: str, bad: str) -> None:
    assert refused(CentreCreate, {"name": "n", "location": "l", field: bad}) == {field}


def test_a_centre_refuses_fields_it_does_not_own() -> None:
    assert refused(CentreCreate, {"name": "n", "location": "l", "is_active": False}) == {
        "is_active"
    }


def test_a_centre_update_may_clear_the_city_but_not_the_name() -> None:
    assert CentreUpdate.model_validate({"city": None}).changes() == {"city": None}
    assert refused(CentreUpdate, {"name": None}) == {"name"}
    assert refused(CentreUpdate, {"location": None}) == {"location"}
    assert refused(CentreUpdate, {"is_active": None}) == {"is_active"}
    assert refused(CentreUpdate, {}) == {""}


# ---------------------------------------------------------------- diagnostic tests


@pytest.mark.parametrize("code", ["CBC", "HBA1C_2", "AB", "A" * 20])
def test_a_valid_test_code_passes(code: str) -> None:
    assert DiagnosticTestCreate.model_validate({"code": code, "name": "n"}).code == code


@pytest.mark.parametrize("code", ["cbc", "A", "A" * 21, "CB C", "CBC-1", "CBC\n", ""])
def test_a_test_code_must_match_the_database_pattern(code: str) -> None:
    """`CBC\\n` is the sharp one: a `$` anchor would accept a trailing newline, `\\Z` does not."""
    assert refused(DiagnosticTestCreate, {"code": code, "name": "n"}) == {"code"}


def test_a_description_may_hold_line_breaks_and_tabs() -> None:
    text = "First line.\n\tSecond line."
    assert DiagnosticTestCreate.model_validate({"code": "CBC", "name": "n", "description": text})
    assert refused(DiagnosticTestCreate, {"code": "CBC", "name": "n", "description": "a\x00b"}) == {
        "description"
    }


def test_a_description_has_a_length_limit() -> None:
    body = {"code": "CBC", "name": "n"}
    assert DiagnosticTestCreate.model_validate({**body, "description": "x" * 1000})
    assert refused(DiagnosticTestCreate, {**body, "description": "x" * 1001}) == {"description"}


def test_a_test_update_may_clear_the_description_but_not_the_code() -> None:
    assert DiagnosticTestUpdate.model_validate({"description": None}).changes() == {
        "description": None
    }
    assert refused(DiagnosticTestUpdate, {"code": None}) == {"code"}
    assert refused(DiagnosticTestUpdate, {"name": None}) == {"name"}


# ---------------------------------------------------------------- prices


@pytest.mark.parametrize("price", ["0.01", "1", "499.00", "499.5", "99999999.99", 499, 499.5])
def test_a_price_inside_the_bounds_passes(price: Any) -> None:
    assert CentreTestWrite.model_validate({"price": price}).price >= Decimal("0.01")


@pytest.mark.parametrize(
    "price",
    [
        "0",
        "0.00",
        "-1",
        "0.009",
        "499.001",
        "100000000",
        "99999999.991",
        "NaN",
        "Infinity",
        "abc",
        "",
        None,
    ],
)
def test_a_price_outside_the_bounds_is_refused(price: Any) -> None:
    """Three decimals, zero, NaN and anything past numeric(10,2) never reach the database."""
    assert refused(CentreTestWrite, {"price": price}) == {"price"}


def test_a_price_is_required() -> None:
    assert refused(CentreTestWrite, {}) == {"price"}


def test_the_defaults_are_inr_and_available() -> None:
    write = CentreTestWrite.model_validate({"price": "10"})
    assert (write.currency, write.is_available) == ("INR", True)


@pytest.mark.parametrize("currency", ["inr", "Inr", "INRR", "IN", "I", "1NR", "", "R₹P"])
def test_a_currency_must_be_three_upper_case_letters(currency: str) -> None:
    """The rule of ck_centre_tests_currency. `"inr"` used to reach the CHECK and give a 500."""
    assert refused(CentreTestWrite, {"price": "10", "currency": currency}) == {"currency"}


def test_another_iso_shaped_currency_passes() -> None:
    assert CentreTestWrite.model_validate({"price": "10", "currency": "USD"}).currency == "USD"


def test_a_price_body_refuses_extra_fields() -> None:
    assert refused(CentreTestWrite, {"price": "10", "id": "x"}) == {"id"}

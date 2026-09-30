"""PatchModel: a PATCH applies only the fields that were sent, never a null by accident (AB33)."""

from typing import ClassVar

import pytest
from pydantic import ValidationError

from app.core.schemas import PatchModel


class ExampleUpdate(PatchModel):
    nullable: ClassVar[frozenset[str]] = frozenset({"city"})

    name: str | None = None
    city: str | None = None
    is_active: bool | None = None


def error_locations(payload: object) -> list[tuple[str | int, ...]]:
    with pytest.raises(ValidationError) as error:
        ExampleUpdate.model_validate(payload)
    return [item["loc"] for item in error.value.errors()]


def test_only_the_sent_fields_are_changes() -> None:
    assert ExampleUpdate.model_validate({"name": "New"}).changes() == {"name": "New"}
    both = ExampleUpdate.model_validate({"name": "New", "is_active": False}).changes()
    assert both == {"name": "New", "is_active": False}


def test_a_field_that_is_not_sent_is_not_a_change_even_when_falsy() -> None:
    assert "is_active" not in ExampleUpdate.model_validate({"name": "New"}).changes()
    assert ExampleUpdate.model_validate({"is_active": False}).changes() == {"is_active": False}


@pytest.mark.parametrize("field", ["name", "is_active"])
def test_null_is_refused_for_a_field_that_is_not_nullable(field: str) -> None:
    """Without this guard the null reached the database and gave a 500 (23502)."""
    assert error_locations({field: None}) == [(field,)]


def test_null_is_a_change_for_a_nullable_field() -> None:
    assert ExampleUpdate.model_validate({"city": None}).changes() == {"city": None}


def test_an_empty_body_is_refused() -> None:
    assert error_locations({}) == [()]


def test_an_unknown_field_is_refused() -> None:
    assert error_locations({"name": "x", "role": "ADMIN"}) == [("role",)]


def test_a_wrong_type_is_still_refused() -> None:
    assert error_locations({"is_active": "maybe"}) == [("is_active",)]


def test_a_nullable_field_alone_is_a_valid_body() -> None:
    assert ExampleUpdate.model_validate({"city": None}).model_fields_set == {"city"}

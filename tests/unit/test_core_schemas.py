"""The shared text types: trimmed, non-empty, free of control characters (architecture §5.1)."""

from typing import Annotated

import pytest
from pydantic import Field, TypeAdapter, ValidationError

from app.core.schemas import SafeParagraph, SafeText

name = TypeAdapter(Annotated[SafeText, Field(max_length=10)])
paragraph = TypeAdapter(Annotated[SafeParagraph, Field(max_length=50)])


def test_text_is_trimmed() -> None:
    assert name.validate_python("  Jane ") == "Jane"


@pytest.mark.parametrize("value", ["", " ", "   ", "\N{NO-BREAK SPACE}"])
def test_text_that_is_empty_after_trimming_is_refused(value: str) -> None:
    """A min_length placed after an AfterValidator once let "   " through."""
    with pytest.raises(ValidationError):
        name.validate_python(value)


def test_the_length_limit_counts_characters_after_trimming() -> None:
    assert name.validate_python("  " + "a" * 10 + "  ") == "a" * 10
    with pytest.raises(ValidationError):
        name.validate_python("a" * 11)


@pytest.mark.parametrize(
    "value",
    ["a\x00b", "a\tb", "a\nb", "a\rb", "a\x7fb", "a\x85b", "a\N{RIGHT-TO-LEFT OVERRIDE}b"],
)
def test_control_characters_are_refused_inside_text(value: str) -> None:
    with pytest.raises(ValidationError):
        name.validate_python(value)


def test_a_zero_width_joiner_and_accents_are_allowed() -> None:
    assert name.validate_python("ra\N{ZERO WIDTH JOINER}ju")
    assert name.validate_python("Jos\N{LATIN SMALL LETTER E WITH ACUTE}")


def test_a_paragraph_may_hold_tabs_and_line_breaks_but_no_other_control() -> None:
    assert paragraph.validate_python("first line\n\tsecond line") == "first line\n\tsecond line"
    with pytest.raises(ValidationError):
        paragraph.validate_python("first\x00line")
    with pytest.raises(ValidationError):
        paragraph.validate_python("first\rline")  # a bare carriage return is still a control

"""Input types shared by every schema (architecture §5.1)."""

import unicodedata
from decimal import Decimal
from functools import partial
from typing import Annotated, Any, ClassVar, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationInfo,
    field_validator,
    model_validator,
)

# The bidirectional override characters can disguise a name in a log or on a screen.
BIDI_CONTROLS = frozenset(chr(code) for code in (*range(0x202A, 0x202F), *range(0x2066, 0x206A)))


def reject_control_characters(value: str, allow: str = "") -> str:
    """Refuse NUL, TAB, LF, DEL and the other controls (Unicode category Cc), the bidi overrides
    and lone surrogates (category Cs).

    PostgreSQL refuses NUL in `text`, so it would otherwise reach the database as an error.
    A lone surrogate has no UTF-8 form: Python's JSON reader accepts `"\\ud800"`, `SecretStr`
    keeps it, and the hash or the insert then raises. A zero-width joiner stays legal: some
    scripts need it.
    """
    for character in value:
        if character in allow:
            continue
        if unicodedata.category(character) in ("Cc", "Cs") or character in BIDI_CONTROLS:
            raise ValueError("control characters and lone surrogates are not allowed")
    return value


# Trim first, refuse an empty result, then look for control characters. The order matters: a
# constraint placed after an AfterValidator does not always see the trimmed value (min_length
# let "   " through). A schema adds its limit: `Annotated[SafeText, Field(max_length=100)]`.
SafeText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
    AfterValidator(reject_control_characters),
]
# A description may hold line breaks and tabs.
SafeParagraph = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
    AfterValidator(partial(reject_control_characters, allow="\t\n")),
]

# The bounds are those of the money CHECKs and of numeric(10,2), so the database never rounds and a
# client cannot reach a CHECK. NaN and infinity are refused by default.
Money = Annotated[
    Decimal,
    Field(ge=Decimal("0.01"), le=Decimal("99999999.99"), max_digits=10, decimal_places=2),
]
# The same rule as the `ck_*_currency` CHECKs, so a lower-case code is a 422 and not a 500.
CurrencyCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]


class PatchModel(BaseModel):
    """The base of every update model. A PATCH applies only the fields that the client sent.

    Three rules protect the database (architecture §5.1). An unknown field is a 422. An empty body
    is a 422. An explicit `null` is a 422 for every field that is not listed in `nullable`, because
    without this guard `{"name": null}` reached the database and gave a 500.
    Every field of a subclass has the default `None`.
    """

    model_config = ConfigDict(extra="forbid")
    nullable: ClassVar[frozenset[str]] = frozenset()

    @field_validator("*", mode="before")
    @classmethod
    def refuse_null(cls, value: Any, info: ValidationInfo) -> Any:
        if value is None and info.field_name not in cls.nullable:
            raise ValueError("must not be null")
        return value

    @model_validator(mode="after")
    def refuse_an_empty_body(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("send at least one field")
        return self

    def changes(self) -> dict[str, Any]:
        """The fields that the client sent, and only those."""
        return self.model_dump(exclude_unset=True)

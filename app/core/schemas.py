"""Input types shared by every schema (spec section 7.1)."""

import unicodedata
from functools import partial
from typing import Annotated

from pydantic import AfterValidator, StringConstraints

# The bidirectional override characters can disguise a name in a log or on a screen.
BIDI_CONTROLS = frozenset(chr(code) for code in (*range(0x202A, 0x202F), *range(0x2066, 0x206A)))


def reject_control_characters(value: str, allow: str = "") -> str:
    """Refuse NUL, TAB, LF, DEL and the other controls (Unicode category Cc) and the bidi overrides.

    PostgreSQL refuses NUL in `text`, so it would otherwise reach the database as an error.
    A zero-width joiner stays legal: some scripts need it.
    """
    for character in value:
        if character in allow:
            continue
        if unicodedata.category(character) == "Cc" or character in BIDI_CONTROLS:
            raise ValueError("control characters are not allowed")
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

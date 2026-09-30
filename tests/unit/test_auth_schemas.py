"""Signup, login and token input: normalisation, bounds and what is refused (AB6, AB25)."""

from typing import Any

import pytest
from pydantic import ValidationError

from app.auth.schemas import LoginRequest, SignupRequest, TokenForm, UserRead

PASSWORD = "correct horse battery staple"


def signup(**overrides: Any) -> SignupRequest:
    return SignupRequest.model_validate(
        {"email": "jane@example.in", "password": PASSWORD, **overrides}
    )


def rejected_fields(**overrides: Any) -> set[str]:
    with pytest.raises(ValidationError) as error:
        signup(**overrides)
    return {str(item["loc"][0]) for item in error.value.errors()}


# ---------------------------------------------------------------- signup


def test_a_signup_email_is_trimmed_and_lower_cased() -> None:
    assert signup(email="  Jane.Doe@Example.IN ").email == "jane.doe@example.in"


@pytest.mark.parametrize("email", ["", "not-an-email", "a@", "@example.in", "a b@example.in"])
def test_a_malformed_signup_email_is_refused(email: str) -> None:
    assert rejected_fields(email=email) == {"email"}


def test_a_reserved_domain_is_refused_at_signup() -> None:
    """email-validator refuses .local and .test, so a seed admin cannot use them."""
    assert rejected_fields(email="admin@eve.local") == {"email"}


def test_a_password_keeps_its_edge_spaces() -> None:
    assert signup(password="  a password with edge spaces  ").password.get_secret_value() == (
        "  a password with edge spaces  "
    )


def test_a_password_of_128_characters_passes_and_129_fails() -> None:
    assert signup(password="p" * 128)
    assert rejected_fields(password="p" * 129) == {"password"}


def test_an_empty_password_is_refused() -> None:
    assert rejected_fields(password="") == {"password"}


@pytest.mark.parametrize(
    "control",
    ["\x00", "\t", "\n", "\x7f", "\x85", "\N{RIGHT-TO-LEFT OVERRIDE}", "\N{RIGHT-TO-LEFT ISOLATE}"],
)
def test_a_control_character_is_refused_in_a_password(control: str) -> None:
    assert rejected_fields(password=f"long enough {control} password") == {"password"}


@pytest.mark.parametrize("code_point", [0xD800, 0xDBFF, 0xDC00, 0xDFFF])
def test_a_lone_surrogate_is_refused_in_every_text_input(code_point: int) -> None:
    """It cannot be encoded as UTF-8. A `SecretStr` let it through, and hashing raised a 500."""
    surrogate = chr(code_point)
    assert rejected_fields(password=f"long enough {surrogate} password") == {"password"}
    assert rejected_fields(full_name=f"Jane{surrogate}") == {"full_name"}
    with pytest.raises(ValidationError):
        LoginRequest(email="a@example.in", password=surrogate)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        TokenForm(username="a@example.in", password=surrogate)  # type: ignore[arg-type]


def test_a_role_field_is_refused_so_nobody_signs_up_as_admin() -> None:
    """AB6."""
    assert rejected_fields(role="ADMIN") == {"role"}


def test_extra_fields_are_refused() -> None:
    assert rejected_fields(is_admin=True, id="x") == {"is_admin", "id"}


def test_the_password_never_shows_in_the_repr() -> None:
    assert PASSWORD not in repr(signup())


def test_a_name_is_trimmed_bounded_and_free_of_control_characters() -> None:
    assert signup(full_name="  Jane Doe ").full_name == "Jane Doe"
    assert signup().full_name is None
    assert rejected_fields(full_name="") == {"full_name"}
    assert rejected_fields(full_name="   ") == {"full_name"}
    assert rejected_fields(full_name="x" * 101) == {"full_name"}
    assert rejected_fields(full_name="Jane\x00Doe") == {"full_name"}
    assert rejected_fields(full_name="Jane\N{RIGHT-TO-LEFT OVERRIDE}Doe") == {"full_name"}


def test_a_name_may_hold_a_zero_width_joiner() -> None:
    conjunct = (
        "\N{DEVANAGARI LETTER KA}\N{DEVANAGARI SIGN VIRAMA}"
        "\N{ZERO WIDTH JOINER}\N{DEVANAGARI LETTER SSA}"
    )
    assert signup(full_name=conjunct).full_name == conjunct


@pytest.mark.parametrize("phone", ["+919876543210", "+14155552671", "+4420"])
def test_an_e164_phone_number_passes(phone: str) -> None:
    assert signup(phone=phone).phone == phone


@pytest.mark.parametrize(
    "phone", ["9876543210", "+0123456", "+", "+91 98765 43210", "+1" + "2" * 15]
)
def test_a_phone_number_that_is_not_e164_is_refused(phone: str) -> None:
    assert rejected_fields(phone=phone) == {"phone"}


# ---------------------------------------------------------------- login


def test_a_login_email_is_a_plain_string_that_is_trimmed_and_lower_cased() -> None:
    assert LoginRequest(email="  Admin@Eve.Local ", password="x").email == "admin@eve.local"


def test_a_login_email_is_normalised_to_nfc() -> None:
    composed = "caf\N{LATIN SMALL LETTER E WITH ACUTE}@example.in"
    decomposed = "cafe\N{COMBINING ACUTE ACCENT}@example.in"
    assert LoginRequest(email=decomposed, password="x").email == composed


@pytest.mark.parametrize("email", ["", "   ", "a" * 255])
def test_a_login_email_has_one_to_254_characters(email: str) -> None:
    with pytest.raises(ValidationError):
        LoginRequest(email=email, password="x")


def test_a_login_password_needs_no_minimum() -> None:
    """An account made under a longer minimum must still be able to log in."""
    assert LoginRequest(email="a@example.in", password="x").password.get_secret_value() == "x"


def test_a_login_refuses_extra_fields() -> None:
    with pytest.raises(ValidationError):
        LoginRequest.model_validate({"email": "a@example.in", "password": "x", "role": "ADMIN"})


# ---------------------------------------------------------------- token form


def test_the_token_form_ignores_the_other_oauth2_fields() -> None:
    form = TokenForm.model_validate(
        {
            "grant_type": "password",
            "username": "A@Example.in",
            "password": "x",
            "scope": "",
            "client_id": "",
        }
    )
    assert form.username == "a@example.in"


# ---------------------------------------------------------------- response


def test_the_user_response_has_no_password_hash() -> None:
    assert "password_hash" not in UserRead.model_fields
    assert "password" not in UserRead.model_fields

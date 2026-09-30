"""Request and response models of the auth endpoints (architecture §5.6)."""

import unicodedata
import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    EmailStr,
    Field,
    SecretStr,
    StringConstraints,
)

from app.core.schemas import SafeText, reject_control_characters
from app.models.user import Role

MAX_PASSWORD_LENGTH = 128


def _strip_email(value: object) -> object:
    return value.strip() if isinstance(value, str) else value


def _refuse_control_characters_in_secret(password: SecretStr) -> SecretStr:
    reject_control_characters(password.get_secret_value())
    return password


# The minimum length is a setting (PASSWORD_MIN_LENGTH), so the signup gate checks it.
# A password is never stripped: a leading or trailing space is part of the secret.
Password = Annotated[
    SecretStr,
    Field(min_length=1, max_length=MAX_PASSWORD_LENGTH),
    AfterValidator(_refuse_control_characters_in_secret),
]

# A new account needs an address that email-validator accepts, stored in lower case.
SignupEmail = Annotated[EmailStr, BeforeValidator(_strip_email), AfterValidator(str.lower)]


def _normalise_login_email(value: str) -> str:
    return unicodedata.normalize("NFC", value).lower()


# A login accepts any string that a stored address can be, so it is not an EmailStr: an account
# that signup would refuse (a seeded admin at a reserved domain) must still be able to log in.
LoginEmail = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=254),
    AfterValidator(_normalise_login_email),
]


class SignupRequest(BaseModel):
    # A `role` field is a 422, so nobody signs up as admin.
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "email": "jane@example.in",
                    "password": "a long password of 15 or more characters",
                    "full_name": "Jane Doe",
                    "phone": "+919812345678",
                }
            ]
        },
    )

    email: SignupEmail
    password: Password
    full_name: Annotated[SafeText, Field(max_length=100)] | None = None
    phone: Annotated[str, StringConstraints(pattern=r"^\+[1-9]\d{1,14}$")] | None = None  # E.164


class LoginRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {"email": "jane@example.in", "password": "a long password of 15 or more characters"}
            ]
        },
    )

    email: LoginEmail
    password: Password


class TokenForm(BaseModel):
    """The OAuth2 password form that Swagger's Authorize button posts to `/auth/token`."""

    model_config = ConfigDict(extra="ignore")  # the form also sends grant_type, scope and more

    username: LoginEmail
    password: Password


class TokenRead(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105  # the OAuth2 token type, not a password
    expires_in: int = Field(description="Seconds until the token expires.")


class UserRead(BaseModel):
    """A user without the password hash."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    full_name: str | None
    phone: str | None
    role: Role
    created_at: datetime

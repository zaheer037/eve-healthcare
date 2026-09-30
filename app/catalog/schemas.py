"""Request and response models of the catalog endpoints (spec section 7.6)."""

import uuid
from decimal import Decimal
from typing import Annotated, ClassVar

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.core.schemas import CurrencyCode, Money, PatchModel, SafeParagraph, SafeText

CentreName = Annotated[SafeText, Field(max_length=120)]
CentreLocation = Annotated[SafeText, Field(max_length=200)]
CentreCity = Annotated[SafeText, Field(max_length=80)]
TestCode = Annotated[str, StringConstraints(pattern=r"^[A-Z0-9_]{2,20}$")]
TestName = Annotated[SafeText, Field(max_length=120)]
TestDescription = Annotated[SafeParagraph, Field(max_length=1000)]


# ---------------------------------------------------------------- centres


class CentreCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: CentreName
    location: CentreLocation
    city: CentreCity | None = None


class CentreUpdate(PatchModel):
    nullable: ClassVar[frozenset[str]] = frozenset({"city"})

    name: CentreName | None = None
    location: CentreLocation | None = None
    city: CentreCity | None = None
    is_active: bool | None = None  # `true` restores a deactivated centre


class CentreRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    location: str
    city: str | None
    is_active: bool
    price: Decimal | None = Field(
        default=None,
        description="The centre's price for `test_id`. It is set only when the list filters by it.",
    )


class CentreTestRead(BaseModel):
    """A test as one centre offers it."""

    model_config = ConfigDict(from_attributes=True)

    test_id: uuid.UUID
    code: str
    name: str
    price: Decimal
    currency: str
    is_available: bool


class CentreDetailRead(CentreRead):
    tests: list[CentreTestRead]


class CentreTestWrite(BaseModel):
    """The body of the price upsert: a full value, so the same body always gives the same state."""

    model_config = ConfigDict(extra="forbid")

    price: Money
    currency: CurrencyCode = "INR"
    is_available: bool = True


# ---------------------------------------------------------------- diagnostic tests


class DiagnosticTestCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: TestCode
    name: TestName
    description: TestDescription | None = None


class DiagnosticTestUpdate(PatchModel):
    nullable: ClassVar[frozenset[str]] = frozenset({"description"})

    code: TestCode | None = None
    name: TestName | None = None
    description: TestDescription | None = None
    is_active: bool | None = None


class DiagnosticTestRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    description: str | None
    is_active: bool

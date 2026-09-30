"""Public reads and admin writes for centres and diagnostic tests (architecture §5.2).

Each resource has a public router and an admin router. The admin router carries
`require_admin` on the router itself, so a new admin endpoint cannot ship without the check.
"""

import uuid
from decimal import Decimal
from typing import Annotated

from fastapi import Depends, Query, Request, Response

from app.auth.dependencies import require_admin
from app.catalog.repository import CentreFilters, OfferedTest
from app.catalog.schemas import (
    CentreCreate,
    CentreDetailRead,
    CentreRead,
    CentreTestRead,
    CentreTestWrite,
    CentreUpdate,
    DiagnosticTestCreate,
    DiagnosticTestRead,
    DiagnosticTestUpdate,
)
from app.catalog.service import CatalogService
from app.core.db import SessionDep
from app.core.errors import (
    CentreNotFound,
    DiagnosticTestNotFound,
    DuplicateCentre,
    DuplicateDiagnosticTest,
    Forbidden,
    InvalidToken,
    NotAuthenticated,
    ValidationFailed,
)
from app.core.http import located
from app.core.pagination import Page, PageParamsDep
from app.core.problem import problems
from app.core.routing import TolerantRouter
from app.core.schemas import SafeText
from app.models.catalog import Centre, DiagnosticTest

centres_public = TolerantRouter(prefix="/centres", tags=["centres"])
centres_admin = TolerantRouter(
    prefix="/centres", tags=["centres"], dependencies=[Depends(require_admin)]
)
tests_public = TolerantRouter(prefix="/tests", tags=["tests"])
tests_admin = TolerantRouter(prefix="/tests", tags=["tests"], dependencies=[Depends(require_admin)])


# Who can write: a missing or bad token, or a signed-in user who is not an admin.
ADMIN_ONLY = (NotAuthenticated, InvalidToken, Forbidden)


def get_catalog_service(session: SessionDep) -> CatalogService:
    return CatalogService(session)


CatalogServiceDep = Annotated[CatalogService, Depends(get_catalog_service)]

# Query text is trimmed and free of control characters, like every other input.
CityQuery = Annotated[SafeText, Query(max_length=80, description="Exact match, any case.")]
SearchQuery = Annotated[SafeText, Query(max_length=100, description="A substring, any case.")]


def centre_read(centre: Centre, price: Decimal | None = None) -> CentreRead:
    return CentreRead.model_validate(centre).model_copy(update={"price": price})


def centre_detail(centre: Centre, offered: list[OfferedTest]) -> CentreDetailRead:
    return CentreDetailRead(
        **centre_read(centre).model_dump(),
        tests=[CentreTestRead.model_validate(test) for test in offered],
    )


def diagnostic_test_read(test: DiagnosticTest) -> DiagnosticTestRead:
    return DiagnosticTestRead.model_validate(test)


# ================================================================ centres (public)


@centres_public.get("/", summary="List the active centres", responses=problems(ValidationFailed))
def list_centres(
    service: CatalogServiceDep,
    page: PageParamsDep,
    city: CityQuery | None = None,
    test_id: uuid.UUID | None = None,
    q: SearchQuery | None = None,
) -> Page[CentreRead]:
    """Filter by `city`, by `test_id` (then each centre carries its price) or by text `q`."""
    rows, total = service.list_centres(CentreFilters(city=city, test_id=test_id, q=q), page)
    return Page[CentreRead](
        data=[centre_read(centre, price) for centre, price in rows],
        pagination=page.describe(total),
    )


@centres_public.get(
    "/{centre_id}",
    summary="One centre with the tests it offers",
    responses=problems(CentreNotFound, ValidationFailed),
)
def get_centre(centre_id: uuid.UUID, service: CatalogServiceDep) -> CentreDetailRead:
    """An active centre with the tests that it offers now and their prices. An inactive centre
    answers `404`, as an unknown one does."""
    centre, offered = service.get_centre(centre_id)
    return centre_detail(centre, offered)


# ================================================================ centres (admin)


@centres_admin.post(
    "/",
    status_code=201,
    summary="Create a centre",
    responses=problems(*ADMIN_ONLY, DuplicateCentre, ValidationFailed),
)
def create_centre(
    data: CentreCreate, service: CatalogServiceDep, request: Request, response: Response
) -> CentreRead:
    """Add an active centre. Two centres cannot share a name and a location."""
    centre = service.create_centre(name=data.name, location=data.location, city=data.city)
    located(request, response, "get_centre", centre_id=centre.id)
    return centre_read(centre)


@centres_admin.patch(
    "/{centre_id}",
    summary="Change some fields of a centre, or restore it",
    responses=problems(*ADMIN_ONLY, CentreNotFound, DuplicateCentre, ValidationFailed),
)
def update_centre(
    centre_id: uuid.UUID, data: CentreUpdate, service: CatalogServiceDep
) -> CentreRead:
    """Send only the fields to change. `{"is_active": true}` restores a deactivated centre."""
    return centre_read(service.update_centre(centre_id, data.changes()))


@centres_admin.delete(
    "/{centre_id}",
    status_code=204,
    summary="Deactivate a centre",
    responses=problems(*ADMIN_ONLY, CentreNotFound, ValidationFailed),
)
def deactivate_centre(centre_id: uuid.UUID, service: CatalogServiceDep) -> None:
    """A centre is never deleted, because bookings refer to it. A repeated call is not an error."""
    service.deactivate_centre(centre_id)


@centres_admin.put(
    "/{centre_id}/tests/{test_id}",
    summary="Set the price of a test at a centre",
    responses={
        **problems(*ADMIN_ONLY, CentreNotFound, DiagnosticTestNotFound, ValidationFailed),
        201: {"model": CentreTestRead, "description": "The centre did not offer it yet."},
    },
)
def set_centre_test(
    centre_id: uuid.UUID,
    test_id: uuid.UUID,
    data: CentreTestWrite,
    service: CatalogServiceDep,
    response: Response,
) -> CentreTestRead:
    """Send the whole value. The same body always gives the same state. `201` when the centre
    did not offer the test yet and `200` when the call replaced its price."""
    offered, created = service.set_centre_test(
        centre_id=centre_id,
        test_id=test_id,
        price=data.price,
        currency=data.currency,
        is_available=data.is_available,
    )
    if created:
        response.status_code = 201
    return CentreTestRead.model_validate(offered)


# ================================================================ diagnostic tests (public)


@tests_public.get(
    "/", summary="List the active diagnostic tests", responses=problems(ValidationFailed)
)
def list_tests(
    service: CatalogServiceDep, page: PageParamsDep, q: SearchQuery | None = None
) -> Page[DiagnosticTestRead]:
    """Filter by text `q` in the code or the name."""
    tests, total = service.list_tests(q, page)
    return Page[DiagnosticTestRead](
        data=[diagnostic_test_read(test) for test in tests],
        pagination=page.describe(total),
    )


@tests_public.get(
    "/{test_id}",
    summary="One diagnostic test",
    responses=problems(DiagnosticTestNotFound, ValidationFailed),
)
def get_test(test_id: uuid.UUID, service: CatalogServiceDep) -> DiagnosticTestRead:
    """An active diagnostic test. An inactive one answers `404`, as an unknown one does."""
    return diagnostic_test_read(service.get_test(test_id))


# ================================================================ diagnostic tests (admin)


@tests_admin.post(
    "/",
    status_code=201,
    summary="Create a diagnostic test",
    responses=problems(*ADMIN_ONLY, DuplicateDiagnosticTest, ValidationFailed),
)
def create_test(
    data: DiagnosticTestCreate, service: CatalogServiceDep, request: Request, response: Response
) -> DiagnosticTestRead:
    """Add an active test. The code and the name are each unique."""
    test = service.create_test(code=data.code, name=data.name, description=data.description)
    located(request, response, "get_test", test_id=test.id)
    return diagnostic_test_read(test)


@tests_admin.patch(
    "/{test_id}",
    summary="Change some fields of a test, or restore it",
    responses=problems(
        *ADMIN_ONLY, DiagnosticTestNotFound, DuplicateDiagnosticTest, ValidationFailed
    ),
)
def update_test(
    test_id: uuid.UUID, data: DiagnosticTestUpdate, service: CatalogServiceDep
) -> DiagnosticTestRead:
    """Send only the fields to change. `{"is_active": true}` restores a deactivated test."""
    return diagnostic_test_read(service.update_test(test_id, data.changes()))


@tests_admin.delete(
    "/{test_id}",
    status_code=204,
    summary="Deactivate a diagnostic test",
    responses=problems(*ADMIN_ONLY, DiagnosticTestNotFound, ValidationFailed),
)
def deactivate_test(test_id: uuid.UUID, service: CatalogServiceDep) -> None:
    """A test is never deleted, because bookings refer to it. A repeated call is not an error."""
    service.deactivate_test(test_id)

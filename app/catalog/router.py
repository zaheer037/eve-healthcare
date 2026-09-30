"""Public reads and admin writes for centres and diagnostic tests (spec section 7.2).

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
    CentreUpdate,
    DiagnosticTestCreate,
    DiagnosticTestRead,
    DiagnosticTestUpdate,
)
from app.catalog.service import CatalogService
from app.core.db import SessionDep
from app.core.pagination import Page, PageParamsDep
from app.core.routing import TolerantRouter
from app.core.schemas import SafeText
from app.models.catalog import Centre, DiagnosticTest

centres_public = TolerantRouter(prefix="/centres", tags=["centres"])
centres_admin = TolerantRouter(
    prefix="/centres", tags=["centres"], dependencies=[Depends(require_admin)]
)
tests_public = TolerantRouter(prefix="/tests", tags=["tests"])
tests_admin = TolerantRouter(prefix="/tests", tags=["tests"], dependencies=[Depends(require_admin)])


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


def located(request: Request, response: Response, route_name: str, **path_params: object) -> None:
    """Set `Location` to a path. `url_path_for` returns no host, so a hostile Host header cannot
    reach it (sharp edge 47)."""
    response.headers["Location"] = str(request.app.url_path_for(route_name, **path_params))


# ================================================================ centres (public)


@centres_public.get("/", summary="List the active centres")
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


@centres_public.get("/{centre_id}", summary="One centre with the tests it offers")
def get_centre(centre_id: uuid.UUID, service: CatalogServiceDep) -> CentreDetailRead:
    centre, offered = service.get_centre(centre_id)
    return centre_detail(centre, offered)


# ================================================================ centres (admin)


@centres_admin.post("/", status_code=201, summary="Create a centre")
def create_centre(
    data: CentreCreate, service: CatalogServiceDep, request: Request, response: Response
) -> CentreRead:
    centre = service.create_centre(name=data.name, location=data.location, city=data.city)
    located(request, response, "get_centre", centre_id=centre.id)
    return centre_read(centre)


@centres_admin.patch("/{centre_id}", summary="Change some fields of a centre, or restore it")
def update_centre(
    centre_id: uuid.UUID, data: CentreUpdate, service: CatalogServiceDep
) -> CentreRead:
    return centre_read(service.update_centre(centre_id, data.changes()))


@centres_admin.delete("/{centre_id}", status_code=204, summary="Deactivate a centre")
def deactivate_centre(centre_id: uuid.UUID, service: CatalogServiceDep) -> None:
    """A centre is never deleted, because bookings refer to it. A repeated call is not an error."""
    service.deactivate_centre(centre_id)


# ================================================================ diagnostic tests (public)


@tests_public.get("/", summary="List the active diagnostic tests")
def list_tests(
    service: CatalogServiceDep, page: PageParamsDep, q: SearchQuery | None = None
) -> Page[DiagnosticTestRead]:
    """Filter by text `q` in the code or the name."""
    tests, total = service.list_tests(q, page)
    return Page[DiagnosticTestRead](
        data=[diagnostic_test_read(test) for test in tests],
        pagination=page.describe(total),
    )


@tests_public.get("/{test_id}", summary="One diagnostic test")
def get_test(test_id: uuid.UUID, service: CatalogServiceDep) -> DiagnosticTestRead:
    return diagnostic_test_read(service.get_test(test_id))


# ================================================================ diagnostic tests (admin)


@tests_admin.post("/", status_code=201, summary="Create a diagnostic test")
def create_test(
    data: DiagnosticTestCreate, service: CatalogServiceDep, request: Request, response: Response
) -> DiagnosticTestRead:
    test = service.create_test(code=data.code, name=data.name, description=data.description)
    located(request, response, "get_test", test_id=test.id)
    return diagnostic_test_read(test)


@tests_admin.patch("/{test_id}", summary="Change some fields of a test, or restore it")
def update_test(
    test_id: uuid.UUID, data: DiagnosticTestUpdate, service: CatalogServiceDep
) -> DiagnosticTestRead:
    return diagnostic_test_read(service.update_test(test_id, data.changes()))


@tests_admin.delete("/{test_id}", status_code=204, summary="Deactivate a diagnostic test")
def deactivate_test(test_id: uuid.UUID, service: CatalogServiceDep) -> None:
    service.deactivate_test(test_id)

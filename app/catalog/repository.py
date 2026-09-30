"""Catalog queries. SQL only: no business rule and no commit (spec section 4.2)."""

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, func, null, or_, select, update
from sqlalchemy.orm import QueryableAttribute, Session

from app.core.pagination import PageParams
from app.models.catalog import Centre, CentreTest, DiagnosticTest


def escape_like(value: str) -> str:
    """Escape `\\`, `%` and `_`, so a client's text matches literally inside an ILIKE pattern."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def contains(column: QueryableAttribute[Any], text: str) -> Any:
    """A case-insensitive substring match with the client's wildcards escaped."""
    return column.ilike(f"%{escape_like(text)}%", escape="\\")


@dataclass(frozen=True, slots=True)
class CentreFilters:
    city: str | None = None
    test_id: uuid.UUID | None = None
    q: str | None = None


@dataclass(frozen=True, slots=True)
class OfferedTest:
    """A test as one centre offers it: the identity of the test and the centre's price for it."""

    test_id: uuid.UUID
    code: str
    name: str
    price: Decimal
    currency: str
    is_available: bool


class CatalogRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    # ---------------------------------------------------------------- centres

    @staticmethod
    def _filter_centres[Row: tuple[Any, ...]](
        stmt: Select[Row], filters: CentreFilters
    ) -> Select[Row]:
        """Active centres that match. With a test: only centres that offer it, with its price."""
        stmt = stmt.where(Centre.is_active)
        if filters.test_id is not None:
            stmt = (
                stmt.join(CentreTest, CentreTest.centre_id == Centre.id)
                .join(DiagnosticTest, DiagnosticTest.id == CentreTest.test_id)
                .where(
                    CentreTest.test_id == filters.test_id,
                    CentreTest.is_available,
                    DiagnosticTest.is_active,
                )
            )
        if filters.city is not None:
            # lower(city) on active rows is what the partial index ix_centres_city_active holds.
            stmt = stmt.where(func.lower(Centre.city) == filters.city.lower())
        if filters.q is not None:
            stmt = stmt.where(
                or_(
                    contains(Centre.name, filters.q),
                    contains(Centre.location, filters.q),
                    contains(Centre.city, filters.q),
                )
            )
        return stmt

    def count_centres(self, filters: CentreFilters) -> int:
        stmt = self._filter_centres(select(func.count()).select_from(Centre), filters)
        return self._session.scalar(stmt) or 0

    def list_centres(
        self, filters: CentreFilters, page: PageParams
    ) -> list[tuple[Centre, Decimal | None]]:
        """A page of centres in a fixed order. The price is the centre's price for `test_id`."""
        price = CentreTest.price if filters.test_id is not None else null()
        stmt = (
            self._filter_centres(select(Centre, price), filters)
            .order_by(Centre.name, Centre.id)
            .limit(page.limit)
            .offset(page.offset)
        )
        return [(centre, cost) for centre, cost in self._session.execute(stmt)]

    def get_centre(self, centre_id: uuid.UUID, *, only_active: bool = True) -> Centre | None:
        centre = self._session.get(Centre, centre_id)
        if centre is None or (only_active and not centre.is_active):
            return None
        return centre

    def offered_tests(self, centre_id: uuid.UUID) -> list[OfferedTest]:
        """The available, active tests of a centre with its prices, in a fixed order."""
        stmt = (
            select(
                DiagnosticTest.id,
                DiagnosticTest.code,
                DiagnosticTest.name,
                CentreTest.price,
                CentreTest.currency,
                CentreTest.is_available,
            )
            .join(CentreTest, CentreTest.test_id == DiagnosticTest.id)
            .where(
                CentreTest.centre_id == centre_id,
                CentreTest.is_available,
                DiagnosticTest.is_active,
            )
            .order_by(DiagnosticTest.name, DiagnosticTest.id)
        )
        return [OfferedTest(*row) for row in self._session.execute(stmt)]

    def deactivate_centre(self, centre_id: uuid.UUID) -> bool:
        """One statement. It also matches an inactive row, so a repeated call is not an error."""
        stmt = (
            update(Centre)
            .where(Centre.id == centre_id)
            .values(is_active=False)
            .returning(Centre.id)
        )
        return self._session.execute(stmt).first() is not None

    # ---------------------------------------------------------------- diagnostic tests

    @staticmethod
    def _filter_tests[Row: tuple[Any, ...]](stmt: Select[Row], q: str | None) -> Select[Row]:
        stmt = stmt.where(DiagnosticTest.is_active)
        if q is not None:
            stmt = stmt.where(
                or_(contains(DiagnosticTest.code, q), contains(DiagnosticTest.name, q))
            )
        return stmt

    def count_tests(self, q: str | None) -> int:
        stmt = self._filter_tests(select(func.count()).select_from(DiagnosticTest), q)
        return self._session.scalar(stmt) or 0

    def list_tests(self, q: str | None, page: PageParams) -> list[DiagnosticTest]:
        stmt = (
            self._filter_tests(select(DiagnosticTest), q)
            .order_by(DiagnosticTest.name, DiagnosticTest.id)
            .limit(page.limit)
            .offset(page.offset)
        )
        return list(self._session.scalars(stmt))

    def get_test(self, test_id: uuid.UUID, *, only_active: bool = True) -> DiagnosticTest | None:
        test = self._session.get(DiagnosticTest, test_id)
        if test is None or (only_active and not test.is_active):
            return None
        return test

    def deactivate_test(self, test_id: uuid.UUID) -> bool:
        stmt = (
            update(DiagnosticTest)
            .where(DiagnosticTest.id == test_id)
            .values(is_active=False)
            .returning(DiagnosticTest.id)
        )
        return self._session.execute(stmt).first() is not None

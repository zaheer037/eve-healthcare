"""Catalog use cases. The service owns the transaction and ends it before it returns."""

import uuid
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.catalog.repository import CatalogRepository, CentreFilters, OfferedTest
from app.core.db import domain_errors
from app.core.errors import (
    AppError,
    CentreNotFound,
    DiagnosticTestNotFound,
    DuplicateCentre,
    DuplicateDiagnosticTest,
)
from app.core.pagination import PageParams
from app.models.catalog import Centre, DiagnosticTest


class CatalogService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._repository = CatalogRepository(session)

    def _commit(self, duplicates: Mapping[str, AppError]) -> None:
        with domain_errors(self._session, duplicates):
            self._session.commit()

    # ---------------------------------------------------------------- centres

    def list_centres(
        self, filters: CentreFilters, page: PageParams
    ) -> tuple[list[tuple[Centre, Decimal | None]], int]:
        """A page of active centres and the total. Two statements: the count, then the page.

        Raises: nothing.
        Commits: no.
        """
        total = self._repository.count_centres(filters)
        return self._repository.list_centres(filters, page), total

    def get_centre(self, centre_id: uuid.UUID) -> tuple[Centre, list[OfferedTest]]:
        """An active centre with the tests that it offers. Two statements.

        Raises:
            CentreNotFound: no active centre has this ID. An inactive one gives the same answer.

        Commits: no.
        """
        centre = self._repository.get_centre(centre_id)
        if centre is None:  # unknown and inactive give the same answer
            raise CentreNotFound("No centre has this ID.")
        return centre, self._repository.offered_tests(centre_id)

    def create_centre(self, *, name: str, location: str, city: str | None) -> Centre:
        """Create an active centre. One `INSERT ... RETURNING`, and no read first.

        Raises:
            DuplicateCentre: a centre with this name and location exists.

        Commits: yes.
        """
        centre = Centre(name=name, location=location, city=city)
        self._session.add(centre)
        self._commit({"uq_centres_name_location": DuplicateCentre("This centre already exists.")})
        return centre

    def update_centre(self, centre_id: uuid.UUID, changes: Mapping[str, Any]) -> Centre:
        """Apply the fields that the client sent. It also finds an inactive row, so a PATCH can
        restore it. `# ponytail: last write wins -> lock the row with SELECT ... FOR UPDATE if two
        admins edit one centre at once`.

        Raises:
            CentreNotFound: no centre has this ID.
            DuplicateCentre: another centre has the new name and location.

        Commits: yes.
        """
        centre = self._repository.get_centre(centre_id, only_active=False)
        if centre is None:
            raise CentreNotFound("No centre has this ID.")
        for field, value in changes.items():  # the schema allows only the fields of the model
            setattr(centre, field, value)
        self._commit({"uq_centres_name_location": DuplicateCentre("This centre already exists.")})
        return centre

    def deactivate_centre(self, centre_id: uuid.UUID) -> None:
        """Hide a centre. One statement. A repeated call is not an error, because the row is found
        whether it is active or not.

        Raises:
            CentreNotFound: no centre has this ID.

        Commits: yes.
        """
        if not self._repository.deactivate_centre(centre_id):
            raise CentreNotFound("No centre has this ID.")
        self._session.commit()

    # ---------------------------------------------------------------- prices

    def set_centre_test(
        self,
        *,
        centre_id: uuid.UUID,
        test_id: uuid.UUID,
        price: Decimal,
        currency: str,
        is_available: bool,
    ) -> tuple[OfferedTest, bool]:
        """Create or replace the price of a test at a centre. The flag says whether it is new.

        One statement and no read first: the foreign keys check that the centre and the test
        exist. The same call twice leaves the same state, so a client can retry it safely.

        Raises:
            CentreNotFound: no centre has this ID.
            DiagnosticTestNotFound: no test has this ID.

        Commits: yes.
        """
        missing = {
            "fk_centre_tests_centre_id": CentreNotFound("No centre has this ID."),
            "fk_centre_tests_test_id": DiagnosticTestNotFound("No diagnostic test has this ID."),
        }
        with domain_errors(self._session, missing):
            offered, created = self._repository.upsert_centre_test(
                centre_id=centre_id,
                test_id=test_id,
                price=price,
                currency=currency,
                is_available=is_available,
            )
            self._session.commit()
        return offered, created

    # ---------------------------------------------------------------- diagnostic tests

    def list_tests(self, q: str | None, page: PageParams) -> tuple[list[DiagnosticTest], int]:
        """A page of active tests and the total, filtered by text in the code or the name.

        Raises: nothing.
        Commits: no.
        """
        total = self._repository.count_tests(q)
        return self._repository.list_tests(q, page), total

    def get_test(self, test_id: uuid.UUID) -> DiagnosticTest:
        """An active test. One statement.

        Raises:
            DiagnosticTestNotFound: no active test has this ID.

        Commits: no.
        """
        test = self._repository.get_test(test_id)
        if test is None:
            raise DiagnosticTestNotFound("No diagnostic test has this ID.")
        return test

    def create_test(self, *, code: str, name: str, description: str | None) -> DiagnosticTest:
        """Create an active test. One `INSERT ... RETURNING`, and no read first.

        Raises:
            DuplicateDiagnosticTest: a test with this code or this name exists.

        Commits: yes.
        """
        test = DiagnosticTest(code=code, name=name, description=description)
        self._session.add(test)
        self._commit(self._test_duplicates())
        return test

    def update_test(self, test_id: uuid.UUID, changes: Mapping[str, Any]) -> DiagnosticTest:
        """Apply the fields that the client sent. It also finds an inactive row, so a PATCH can
        restore it.

        Raises:
            DiagnosticTestNotFound: no test has this ID.
            DuplicateDiagnosticTest: another test has the new code or name.

        Commits: yes.
        """
        test = self._repository.get_test(test_id, only_active=False)
        if test is None:
            raise DiagnosticTestNotFound("No diagnostic test has this ID.")
        for field, value in changes.items():
            setattr(test, field, value)
        self._commit(self._test_duplicates())
        return test

    def deactivate_test(self, test_id: uuid.UUID) -> None:
        """Hide a test. A repeated call is not an error.

        Raises:
            DiagnosticTestNotFound: no test has this ID.

        Commits: yes.
        """
        if not self._repository.deactivate_test(test_id):
            raise DiagnosticTestNotFound("No diagnostic test has this ID.")
        self._session.commit()

    @staticmethod
    def _test_duplicates() -> dict[str, AppError]:
        return {
            "uq_diagnostic_tests_code": DuplicateDiagnosticTest("A test with this code exists."),
            "uq_diagnostic_tests_name": DuplicateDiagnosticTest("A test with this name exists."),
        }

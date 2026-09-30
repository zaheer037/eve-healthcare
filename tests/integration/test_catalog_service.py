"""The catalog use cases on a real database: filters, paging, soft delete, duplicates (AB17)."""

import uuid
from collections.abc import Callable, Iterator
from decimal import Decimal

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.catalog.repository import CentreFilters, OfferedTest
from app.catalog.service import CatalogService
from app.core.errors import (
    CentreNotFound,
    DiagnosticTestNotFound,
    DuplicateCentre,
    DuplicateDiagnosticTest,
)
from app.core.pagination import PageParams
from app.models.catalog import Centre, CentreTest, DiagnosticTest
from tests.factories import make_centre, make_centre_test, make_diagnostic_test
from tests.support import QueryCounter

NO_FILTERS = CentreFilters()
FIRST_PAGE = PageParams()


@pytest.fixture
def service(db_engine: Engine) -> Iterator[CatalogService]:
    """A service with its own session, as a request has. Arrange through `db_session`."""
    with Session(db_engine, expire_on_commit=False) as session:
        yield CatalogService(session)


def names(rows: list[tuple[Centre, Decimal | None]]) -> list[str]:
    return [centre.name for centre, _ in rows]


# ==== centres: create, read


def test_create_a_centre_with_the_defaults(service: CatalogService) -> None:
    centre = service.create_centre(name="City Lab", location="12 MG Road", city="Pune")
    assert centre.id is not None
    assert centre.is_active is True
    assert centre.created_at is not None


def test_a_centre_needs_no_city(service: CatalogService) -> None:
    assert service.create_centre(name="City Lab", location="12 MG Road", city=None).city is None


def test_a_duplicate_name_and_location_is_refused_and_the_session_stays_usable(
    db_session: Session, service: CatalogService
) -> None:
    make_centre(db_session, name="City Lab", location="12 MG Road")
    with pytest.raises(DuplicateCentre):
        service.create_centre(name="City Lab", location="12 MG Road", city=None)
    assert service.create_centre(
        name="City Lab", location="7 Park Street", city=None
    )  # after a rollback


def test_get_a_centre_with_the_tests_it_offers(
    db_session: Session, service: CatalogService
) -> None:
    centre = make_centre(db_session)
    beta = make_diagnostic_test(db_session, code="BETA", name="Beta test")
    alpha = make_diagnostic_test(db_session, code="ALPHA", name="Alpha test")
    hidden = make_diagnostic_test(db_session, code="HIDDEN", name="Hidden test", is_active=False)
    make_centre_test(db_session, centre=centre, test=beta, price=Decimal("300"))
    make_centre_test(db_session, centre=centre, test=alpha, price=Decimal("200.50"), currency="INR")
    make_centre_test(db_session, centre=centre, test=hidden)
    make_centre_test(
        db_session, centre=centre, test=make_diagnostic_test(db_session), is_available=False
    )
    found, offered = service.get_centre(centre.id)
    assert found.id == centre.id
    assert [(o.code, o.price, o.currency) for o in offered] == [
        ("ALPHA", Decimal("200.50"), "INR"),
        ("BETA", Decimal("300.00"), "INR"),
    ]  # available and active only, ordered by name


def test_an_unknown_centre_is_not_found(service: CatalogService) -> None:
    with pytest.raises(CentreNotFound):
        service.get_centre(uuid.uuid4())


def test_an_inactive_centre_is_not_found_either(
    db_session: Session, service: CatalogService
) -> None:
    centre = make_centre(db_session, is_active=False)
    with pytest.raises(CentreNotFound):
        service.get_centre(centre.id)


# ==== centres: change, restore, deactivate


def test_update_some_fields_of_a_centre(db_session: Session, service: CatalogService) -> None:
    centre = make_centre(db_session, name="Old Name", city="Pune")
    updated = service.update_centre(centre.id, {"name": "New Name", "city": None})
    assert (updated.name, updated.city) == ("New Name", None)
    db_session.refresh(centre)
    assert (centre.name, centre.city, centre.location) == ("New Name", None, "12 MG Road")


def test_renaming_a_centre_onto_another_is_a_duplicate(
    db_session: Session, service: CatalogService
) -> None:
    make_centre(db_session, name="Taken", location="12 MG Road")
    other = make_centre(db_session, name="Free", location="12 MG Road")
    with pytest.raises(DuplicateCentre):
        service.update_centre(other.id, {"name": "Taken"})


def test_an_update_can_restore_an_inactive_centre(
    db_session: Session, service: CatalogService
) -> None:
    centre = make_centre(db_session, is_active=False)
    assert service.update_centre(centre.id, {"is_active": True}).is_active is True
    assert service.get_centre(centre.id)[0].id == centre.id


def test_updating_an_unknown_centre_is_not_found(service: CatalogService) -> None:
    with pytest.raises(CentreNotFound):
        service.update_centre(uuid.uuid4(), {"name": "x"})


def test_deactivating_hides_a_centre_and_keeps_the_row(
    db_session: Session, service: CatalogService
) -> None:
    centre = make_centre(db_session)
    service.deactivate_centre(centre.id)
    assert names(service.list_centres(NO_FILTERS, FIRST_PAGE)[0]) == []
    with pytest.raises(CentreNotFound):
        service.get_centre(centre.id)
    assert db_session.scalar(select(func.count()).select_from(Centre)) == 1  # never deleted


def test_deactivating_twice_is_not_an_error(db_session: Session, service: CatalogService) -> None:
    centre = make_centre(db_session)
    service.deactivate_centre(centre.id)
    service.deactivate_centre(centre.id)


def test_deactivating_an_unknown_centre_is_not_found(service: CatalogService) -> None:
    with pytest.raises(CentreNotFound):
        service.deactivate_centre(uuid.uuid4())


# ==== centres: list, filters, paging


def test_the_list_shows_active_centres_ordered_by_name_and_id(
    db_session: Session, service: CatalogService
) -> None:
    make_centre(db_session, name="Zeta Lab")
    make_centre(db_session, name="Alpha Lab", location="1 First Street")
    make_centre(db_session, name="Alpha Lab", location="2 Second Street")
    make_centre(db_session, name="Hidden Lab", is_active=False)
    rows, total = service.list_centres(NO_FILTERS, FIRST_PAGE)
    assert names(rows) == ["Alpha Lab", "Alpha Lab", "Zeta Lab"]
    assert total == 3
    same_name = [centre for centre, _ in rows[:2]]
    assert same_name == sorted(same_name, key=lambda centre: centre.id)  # the id breaks the tie


def test_the_list_carries_no_price_without_a_test_filter(
    db_session: Session, service: CatalogService
) -> None:
    make_centre_test(db_session)
    (_, price), *_ = service.list_centres(NO_FILTERS, FIRST_PAGE)[0]
    assert price is None


def test_the_city_filter_ignores_case_and_matches_exactly(
    db_session: Session, service: CatalogService
) -> None:
    make_centre(db_session, name="A", city="Pune")
    make_centre(db_session, name="B", city="PUNE")
    make_centre(db_session, name="C", city="Pune East")
    make_centre(db_session, name="D", city=None)
    rows, total = service.list_centres(CentreFilters(city="pUnE"), FIRST_PAGE)
    assert names(rows) == ["A", "B"]
    assert total == 2


def test_the_test_filter_lists_the_centres_that_offer_it_with_the_price(
    db_session: Session, service: CatalogService
) -> None:
    test = make_diagnostic_test(db_session)
    cheap = make_centre(db_session, name="Cheap Lab")
    dear = make_centre(db_session, name="Dear Lab")
    make_centre_test(db_session, centre=cheap, test=test, price=Decimal("100"))
    make_centre_test(db_session, centre=dear, test=test, price=Decimal("900"))
    make_centre(db_session, name="Without It")
    rows, total = service.list_centres(CentreFilters(test_id=test.id), FIRST_PAGE)
    assert [(centre.name, price) for centre, price in rows] == [
        ("Cheap Lab", Decimal("100.00")),
        ("Dear Lab", Decimal("900.00")),
    ]
    assert total == 2


def test_the_test_filter_leaves_out_what_cannot_be_booked(
    db_session: Session, service: CatalogService
) -> None:
    test = make_diagnostic_test(db_session)
    make_centre_test(db_session, centre=make_centre(db_session, name="Bookable"), test=test)
    make_centre_test(
        db_session,
        centre=make_centre(db_session, name="Unavailable"),
        test=test,
        is_available=False,
    )
    make_centre_test(
        db_session, centre=make_centre(db_session, name="Inactive", is_active=False), test=test
    )
    rows, _ = service.list_centres(CentreFilters(test_id=test.id), FIRST_PAGE)
    assert names(rows) == ["Bookable"]
    inactive_test = make_diagnostic_test(db_session, is_active=False)
    make_centre_test(db_session, centre=make_centre(db_session, name="Other"), test=inactive_test)
    assert service.list_centres(CentreFilters(test_id=inactive_test.id), FIRST_PAGE)[1] == 0


def test_the_text_filter_matches_name_location_or_city_in_any_case(
    db_session: Session, service: CatalogService
) -> None:
    make_centre(db_session, name="Sunrise Diagnostics", location="1 A Road", city="Pune")
    make_centre(db_session, name="B", location="5 Sunrise Avenue", city="Pune")
    make_centre(db_session, name="C", location="2 C Road", city="Sunrise Nagar")
    make_centre(db_session, name="D", location="3 D Road", city="Pune")
    assert names(service.list_centres(CentreFilters(q="sunRISE"), FIRST_PAGE)[0]) == [
        "B",
        "C",
        "Sunrise Diagnostics",
    ]


def test_filters_combine(db_session: Session, service: CatalogService) -> None:
    test = make_diagnostic_test(db_session)
    wanted = make_centre(db_session, name="Sunrise Lab", city="Pune")
    make_centre_test(db_session, centre=wanted, test=test)
    make_centre_test(
        db_session, centre=make_centre(db_session, name="Sunrise Two", city="Delhi"), test=test
    )
    make_centre_test(
        db_session, centre=make_centre(db_session, name="Other Lab", city="Pune"), test=test
    )
    rows, total = service.list_centres(
        CentreFilters(city="pune", test_id=test.id, q="sun"), FIRST_PAGE
    )
    assert names(rows) == ["Sunrise Lab"]
    assert total == 1


def test_like_wildcards_in_the_text_filter_match_literally(
    db_session: Session, service: CatalogService
) -> None:
    """AB17: an underscore is not 'any character', and a percent sign is not 'anything'."""
    make_centre(db_session, name="A_B Lab", location="1 Road")
    make_centre(db_session, name="AxB Lab", location="2 Road")
    make_centre(db_session, name="100% Care", location="3 Road")
    make_centre(db_session, name="Plain Lab", location="4 Road")
    make_centre(db_session, name="Back\\slash Lab", location="5 Road")
    assert names(service.list_centres(CentreFilters(q="A_B"), FIRST_PAGE)[0]) == ["A_B Lab"]
    assert names(service.list_centres(CentreFilters(q="100%"), FIRST_PAGE)[0]) == ["100% Care"]
    assert (
        service.list_centres(CentreFilters(q="%"), FIRST_PAGE)[1] == 1
    )  # only the literal percent
    assert names(service.list_centres(CentreFilters(q="k\\s"), FIRST_PAGE)[0]) == [
        "Back\\slash Lab"
    ]


def test_sql_metacharacters_in_the_text_filter_do_nothing(
    db_session: Session, service: CatalogService
) -> None:
    make_centre(db_session, name="Safe Lab")
    hostile = "'; DROP TABLE centres; --"
    assert service.list_centres(CentreFilters(q=hostile), FIRST_PAGE) == ([], 0)
    assert service.list_centres(CentreFilters(city=hostile), FIRST_PAGE) == ([], 0)
    assert names(service.list_centres(NO_FILTERS, FIRST_PAGE)[0]) == [
        "Safe Lab"
    ]  # the table is intact


def test_pages_cover_every_centre_once_in_a_stable_order(
    db_session: Session, service: CatalogService
) -> None:
    for number in range(25):
        make_centre(db_session, name=f"Lab {number % 5}", location=f"{number} Main Road")
    seen: list[uuid.UUID] = []
    for offset in (0, 10, 20):
        rows, total = service.list_centres(NO_FILTERS, PageParams(limit=10, offset=offset))
        assert total == 25
        seen += [centre.id for centre, _ in rows]
    assert len(seen) == len(set(seen)) == 25


def test_a_page_past_the_end_is_empty_and_still_reports_the_total(
    db_session: Session, service: CatalogService
) -> None:
    make_centre(db_session)
    assert service.list_centres(NO_FILTERS, PageParams(limit=10, offset=50)) == ([], 1)


# ==== diagnostic tests


def test_create_and_get_a_test(service: CatalogService) -> None:
    created = service.create_test(
        code="CBC", name="Complete Blood Count", description="Blood.\nAll cells."
    )
    assert service.get_test(created.id).description == "Blood.\nAll cells."


def test_a_duplicate_code_and_a_duplicate_name_are_both_refused(
    db_session: Session, service: CatalogService
) -> None:
    make_diagnostic_test(db_session, code="CBC", name="Complete Blood Count")
    with pytest.raises(DuplicateDiagnosticTest, match="code"):
        service.create_test(code="CBC", name="Something else", description=None)
    with pytest.raises(DuplicateDiagnosticTest, match="name"):
        service.create_test(code="OTHER", name="Complete Blood Count", description=None)


def test_the_test_list_matches_code_or_name_and_hides_inactive_rows(
    db_session: Session, service: CatalogService
) -> None:
    make_diagnostic_test(db_session, code="HBA1C", name="Glycated haemoglobin")
    make_diagnostic_test(db_session, code="TSH", name="Thyroid stimulating hormone")
    make_diagnostic_test(db_session, code="HIDDEN1", name="Hidden", is_active=False)
    rows, total = service.list_tests(None, FIRST_PAGE)
    assert [test.code for test in rows] == ["HBA1C", "TSH"]  # ordered by name
    assert total == 2
    assert [t.code for t in service.list_tests("hba", FIRST_PAGE)[0]] == ["HBA1C"]
    assert [t.code for t in service.list_tests("THYROID", FIRST_PAGE)[0]] == ["TSH"]
    assert service.list_tests("%", FIRST_PAGE) == ([], 0)


def test_update_restore_and_deactivate_a_test(db_session: Session, service: CatalogService) -> None:
    test = make_diagnostic_test(db_session, description="old")
    assert service.update_test(test.id, {"description": None}).description is None
    service.deactivate_test(test.id)
    with pytest.raises(DiagnosticTestNotFound):
        service.get_test(test.id)
    assert service.update_test(test.id, {"is_active": True}).is_active is True
    assert service.get_test(test.id).id == test.id
    service.deactivate_test(test.id)
    service.deactivate_test(test.id)  # twice is fine
    assert db_session.scalar(select(func.count()).select_from(DiagnosticTest)) == 1


def test_an_unknown_test_is_not_found_everywhere(service: CatalogService) -> None:
    missing = uuid.uuid4()
    for action in (
        lambda: service.get_test(missing),
        lambda: service.update_test(missing, {"name": "x"}),
        lambda: service.deactivate_test(missing),
    ):
        with pytest.raises(DiagnosticTestNotFound):
            action()


# ==== statement budgets (architecture §5.2)


def statements_of(db_engine: Engine, action: Callable[[CatalogService], object]) -> int:
    """How many statements one request-like call runs, on a session of its own."""
    with QueryCounter(db_engine) as counter, Session(db_engine, expire_on_commit=False) as session:
        action(CatalogService(session))
    return counter.count


# ==== prices: one upsert statement (T2.4)


def put_price(
    service: CatalogService,
    centre: Centre | uuid.UUID,
    test: DiagnosticTest | uuid.UUID,
    price: str = "499.00",
    *,
    currency: str = "INR",
    is_available: bool = True,
) -> tuple[OfferedTest, bool]:
    return service.set_centre_test(
        centre_id=centre if isinstance(centre, uuid.UUID) else centre.id,
        test_id=test if isinstance(test, uuid.UUID) else test.id,
        price=Decimal(price),
        currency=currency,
        is_available=is_available,
    )


def count_prices(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(CentreTest)) or 0


def test_the_first_call_creates_the_price_and_the_second_updates_it(
    db_session: Session, service: CatalogService
) -> None:
    centre = make_centre(db_session)
    test = make_diagnostic_test(db_session, code="CBC", name="Complete Blood Count")
    offered, created = put_price(service, centre, test, "499")
    assert created is True
    assert offered == OfferedTest(
        test.id, "CBC", "Complete Blood Count", Decimal("499.00"), "INR", True
    )
    offered, created = put_price(
        service, centre, test, "550.50", currency="USD", is_available=False
    )
    assert created is False
    assert (offered.price, offered.currency, offered.is_available) == (
        Decimal("550.50"),
        "USD",
        False,
    )
    assert count_prices(db_session) == 1


def test_the_same_call_twice_leaves_the_same_state(
    db_session: Session, service: CatalogService
) -> None:
    centre, test = make_centre(db_session), make_diagnostic_test(db_session)
    first, _ = put_price(service, centre, test, "120")
    second, created = put_price(service, centre, test, "120")
    assert (first, created) == (second, False)
    assert count_prices(db_session) == 1


def test_an_update_moves_updated_at_and_keeps_created_at(
    db_session: Session, service: CatalogService
) -> None:
    """The upsert bypasses the ORM `onupdate`, so it sets `updated_at` itself."""
    centre, test = make_centre(db_session), make_diagnostic_test(db_session)
    put_price(service, centre, test, "100")
    created_at, updated_at = db_session.execute(
        select(CentreTest.created_at, CentreTest.updated_at)
    ).one()
    assert created_at == updated_at
    put_price(service, centre, test, "200")
    db_session.expire_all()
    later_created_at, later_updated_at = db_session.execute(
        select(CentreTest.created_at, CentreTest.updated_at)
    ).one()
    assert later_created_at == created_at
    assert later_updated_at > updated_at


def test_an_unknown_centre_or_test_is_not_found_and_the_session_stays_usable(
    db_session: Session, service: CatalogService
) -> None:
    centre, test = make_centre(db_session), make_diagnostic_test(db_session)
    with pytest.raises(CentreNotFound):
        put_price(service, uuid.uuid4(), test)
    with pytest.raises(DiagnosticTestNotFound):
        put_price(service, centre, uuid.uuid4())
    assert count_prices(db_session) == 0
    assert put_price(service, centre, test)[1] is True  # after two rollbacks


def test_a_price_can_be_set_while_the_centre_or_the_test_is_inactive(
    db_session: Session, service: CatalogService
) -> None:
    """An admin can prepare a price before a restore. Bookings need both to be active."""
    centre = make_centre(db_session, is_active=False)
    test = make_diagnostic_test(db_session, is_active=False)
    assert put_price(service, centre, test)[1] is True


def test_the_upsert_is_one_statement_when_it_creates_and_when_it_updates(
    db_session: Session, db_engine: Engine
) -> None:
    centre, test = make_centre(db_session), make_diagnostic_test(db_session)
    assert statements_of(db_engine, lambda s: put_price(s, centre, test, "100")) == 1
    assert statements_of(db_engine, lambda s: put_price(s, centre, test, "200")) == 1


def test_a_refused_upsert_is_one_statement_too(
    db_session: Session, db_engine: Engine, service: CatalogService
) -> None:
    test = make_diagnostic_test(db_session)
    with QueryCounter(db_engine) as counter, pytest.raises(CentreNotFound):
        put_price(service, uuid.uuid4(), test)
    assert counter.count == 1  # no read first: the foreign key does the check


def test_the_statement_budgets_hold(db_session: Session, db_engine: Engine) -> None:
    centre = make_centre(db_session)
    test = make_diagnostic_test(db_session)
    make_centre_test(db_session, centre=centre, test=test)
    assert statements_of(db_engine, lambda s: s.list_centres(NO_FILTERS, FIRST_PAGE)) == 2
    assert statements_of(db_engine, lambda s: s.get_centre(centre.id)) == 2  # centre, then tests
    assert statements_of(db_engine, lambda s: s.get_test(test.id)) == 1
    assert statements_of(db_engine, lambda s: s.update_centre(centre.id, {"name": "New"})) == 2
    assert statements_of(db_engine, lambda s: s.deactivate_centre(centre.id)) == 1
    create = lambda s: s.create_centre(name="Brand New", location="1 Road", city=None)  # noqa: E731
    assert statements_of(db_engine, create) == 1  # one INSERT ... RETURNING

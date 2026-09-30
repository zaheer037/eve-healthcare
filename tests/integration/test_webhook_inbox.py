"""The webhook inbox: one row per event, and the gate that makes a duplicate harmless (§6.2)."""

import threading
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.models.payment import WebhookEvent, WebhookOutcome
from app.payments.inbox import WebhookInbox
from app.payments.schemas import WebhookPayload
from tests.factories import make_payment
from tests.support import indexes_used

PROVIDER = "SIMULATOR"


def payload(event_id: str = "evt_1", payment_id: uuid.UUID | None = None) -> WebhookPayload:
    return WebhookPayload.model_validate(
        {
            "event_id": event_id,
            "payment_id": str(payment_id or uuid.uuid4()),
            "status": "SUCCESS",
            "amount": "499.00",
            "provider_payment_id": "sim_1",
            "unknown_field": "dropped",
        }
    )


@pytest.fixture
def session(db_engine: Engine) -> Iterator[Session]:
    with Session(db_engine, expire_on_commit=False) as own:
        yield own


def count_events(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(WebhookEvent)) or 0


# ==== record


def test_a_new_event_is_stored_with_no_outcome_and_no_payment_yet(
    session: Session, db_session: Session
) -> None:
    incoming = payload()
    event_id = WebhookInbox(session).record(PROVIDER, incoming)
    session.commit()
    assert event_id is not None
    stored = db_session.get(WebhookEvent, event_id)
    assert stored is not None
    assert (stored.provider, stored.event_id) == (PROVIDER, "evt_1")
    assert (stored.payment_id, stored.outcome, stored.processed_at) == (None, None, None)
    assert stored.received_at is not None
    assert stored.payload == {
        "event_id": "evt_1",
        "payment_id": str(incoming.payment_id),
        "status": "SUCCESS",
        "provider_payment_id": "sim_1",
        "amount": "499.00",
        "currency": None,
        "failure_reason": None,
    }  # the validated fields only, as plain JSON


def test_the_same_event_again_is_a_duplicate_and_stores_nothing_more(
    session: Session, db_session: Session
) -> None:
    inbox = WebhookInbox(session)
    assert inbox.record(PROVIDER, payload()) is not None
    session.commit()
    assert inbox.record(PROVIDER, payload()) is None
    session.commit()
    assert count_events(db_session) == 1


def test_a_reused_event_id_is_a_duplicate_even_when_the_body_differs(
    session: Session, db_session: Session
) -> None:
    """A ponytail of the design: no payload comparison."""
    inbox = WebhookInbox(session)
    inbox.record(PROVIDER, payload("evt_1"))
    session.commit()
    other_body = WebhookPayload.model_validate(
        {"event_id": "evt_1", "payment_id": str(uuid.uuid4()), "status": "FAILED"}
    )
    assert inbox.record(PROVIDER, other_body) is None


def test_the_same_event_id_from_another_provider_is_a_new_event(session: Session) -> None:
    inbox = WebhookInbox(session)
    assert inbox.record("SIMULATOR", payload()) is not None
    assert inbox.record("OTHER", payload()) is not None
    session.commit()


# ==== mark


def test_marking_an_event_sets_its_outcome_and_its_payment_together(
    session: Session, db_session: Session
) -> None:
    payment = make_payment(db_session)
    inbox = WebhookInbox(session)
    event_id = inbox.record(PROVIDER, payload(payment_id=payment.id))
    assert event_id is not None
    processed_at = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
    inbox.mark(
        event_id,
        outcome=WebhookOutcome.NEEDS_REVIEW,
        detail="terminal_conflict",
        payment_id=payment.id,
        processed_at=processed_at,
    )
    session.commit()
    stored = db_session.get(WebhookEvent, event_id)
    assert stored is not None
    assert stored.outcome is WebhookOutcome.NEEDS_REVIEW
    assert stored.detail == "terminal_conflict"
    assert stored.payment_id == payment.id
    assert stored.processed_at == processed_at


def test_an_event_about_an_unknown_payment_is_marked_without_a_payment(
    session: Session, db_session: Session
) -> None:
    inbox = WebhookInbox(session)
    event_id = inbox.record(PROVIDER, payload())
    assert event_id is not None
    inbox.mark(
        event_id,
        outcome=WebhookOutcome.REJECTED,
        detail="unknown_payment",
        payment_id=None,
        processed_at=datetime.now(UTC),
    )
    session.commit()
    stored = db_session.get(WebhookEvent, event_id)
    assert stored is not None
    assert (stored.outcome, stored.payment_id) == (WebhookOutcome.REJECTED, None)


# ==== the gate under concurrency (architecture §6.2)


def record_in_thread(
    engine: Engine, results: list[uuid.UUID | None], started: threading.Event
) -> None:
    with Session(engine) as own:
        started.set()
        results.append(WebhookInbox(own).record(PROVIDER, payload()))
        own.commit()


@pytest.mark.parametrize("first_commits", [True, False])
def test_a_second_insert_waits_for_the_first_transaction_and_follows_its_fate(
    db_engine: Engine, session: Session, first_commits: bool
) -> None:
    """If the first transaction commits, the second inserts nothing and answers `duplicate`. If it
    rolls back, the second inserts the row and processes the event."""
    first = WebhookInbox(session).record(PROVIDER, payload())
    assert first is not None  # inserted, not yet committed
    results: list[uuid.UUID | None] = []
    started = threading.Event()
    rival = threading.Thread(target=record_in_thread, args=(db_engine, results, started))
    rival.start()
    started.wait(timeout=5)
    time.sleep(0.3)  # the rival is now blocked on the unique index entry of the first insert
    assert results == []
    session.commit() if first_commits else session.rollback()
    rival.join(timeout=5)
    assert len(results) == 1
    assert (results[0] is None) is first_commits


# ==== the index that architecture §3.4 names


def test_the_gate_arbitrates_on_the_unique_event_index(db_engine: Engine) -> None:
    def action() -> None:
        with Session(db_engine) as own:
            WebhookInbox(own).record(PROVIDER, payload())
            own.rollback()

    assert "uq_webhook_events_provider_event" in indexes_used(db_engine, action)

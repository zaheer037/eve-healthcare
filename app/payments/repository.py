"""Payment queries. SQL only: no business rule and no commit (architecture §2.2)."""

import uuid

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.models.booking import Booking, BookingStatus
from app.models.payment import LIVE_PAYMENT_STATUSES, Payment


class PaymentRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def live_payment(self, booking_id: uuid.UUID) -> Payment | None:
        """The booking's `PENDING` or `SUCCESS` payment. The partial unique index
        `uq_payments_live_per_booking` guarantees that there is at most one, and it serves this
        lookup. A caller holds the booking lock, so no writer can change it meanwhile."""
        stmt = select(Payment).where(
            Payment.booking_id == booking_id, Payment.status.in_(LIVE_PAYMENT_STATUSES)
        )
        return self._session.scalar(stmt)

    def add(self, payment: Payment) -> None:
        """Stage a payment. The commit inserts it."""
        self._session.add(payment)

    def booking_id_of(self, payment_id: uuid.UUID) -> uuid.UUID | None:
        """The webhook reads the booking first, because the lock order is booking, then payment."""
        return self._session.scalar(select(Payment.booking_id).where(Payment.id == payment_id))

    def lock(self, payment_id: uuid.UUID) -> Payment:
        """Lock a payment that is known to exist. The caller has locked its booking already.

        A payment is never deleted, so a missing row is a bug and raises `NoResultFound`.

        `FOR NO KEY UPDATE`, because a child insert takes `FOR KEY SHARE` on this row and a plain
        `FOR UPDATE` would conflict with it. `populate_existing`, or the ORM hands
        back a stale object that the session already holds.
        """
        stmt = (
            select(Payment)
            .where(Payment.id == payment_id)
            .with_for_update(key_share=True)
            .execution_options(populate_existing=True)
        )
        return self._session.scalars(stmt).one()

    def reference_held_by_another(self, payment: Payment, provider_payment_id: str) -> bool:
        """Does another payment of this provider already carry this reference?

        Served by `uq_payments_provider_ref`. It cannot see an update that another transaction has
        not committed, so the unique index stays the last word and the webhook retries
        (architecture §6.3).
        """
        stmt = select(
            exists().where(
                Payment.provider == payment.provider,
                Payment.provider_payment_id == provider_payment_id,
                Payment.id != payment.id,
            )
        )
        return bool(self._session.scalar(stmt))

    def get_owned(
        self, payment_id: uuid.UUID, user_id: uuid.UUID
    ) -> tuple[Payment, BookingStatus] | None:
        """A payment of the user's booking, with that booking's current status. One statement."""
        stmt = (
            select(Payment, Booking.status)
            .join(Booking, Booking.id == Payment.booking_id)
            .where(Payment.id == payment_id, Booking.user_id == user_id)
        )
        return self._session.execute(stmt).tuples().first()

    def exists(self, payment_id: uuid.UUID) -> bool:
        """A plain read that tells 403 from 404 after an owner-scoped read found nothing."""
        return bool(self._session.scalar(select(exists().where(Payment.id == payment_id))))

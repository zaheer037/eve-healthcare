"""Bookings (architecture §3.3). The booking is the aggregate root: its payments change only while
its row is locked (architecture §2.2). Rows are never deleted.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import CheckConstraint, ForeignKey, ForeignKeyConstraint, Index, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, str_enum
from app.models.catalog import Centre, DiagnosticTest
from app.models.payment import Payment


class BookingStatus(StrEnum):
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


# A booking in one of these statuses holds its slot and counts against the cap (architecture §5.4).
ACTIVE_STATUSES = (BookingStatus.PENDING, BookingStatus.CONFIRMED)
_ACTIVE_SQL = ", ".join(f"'{status.value}'" for status in ACTIVE_STATUSES)


class Booking(TimestampMixin, Base):
    __tablename__ = "bookings"
    __table_args__ = (
        # One composite key: a booking's test is offered at its centre (architecture §3.5).
        ForeignKeyConstraint(
            ["centre_id", "test_id"],
            ["centre_tests.centre_id", "centre_tests.test_id"],
            name="fk_bookings_centre_test",
            ondelete="RESTRICT",
        ),
        CheckConstraint("amount BETWEEN 0.01 AND 99999999.99", name="amount"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency"),
        # A user holds one active booking per test, centre and start time. Cancelled and failed
        # bookings do not count, so the user can book the slot again.
        Index(
            "uq_bookings_active_slot",
            "user_id",
            "centre_id",
            "test_id",
            "appointment_at",
            unique=True,
            postgresql_where=text(f"status IN ({_ACTIVE_SQL})"),
        ),
        # Serves the foreign key check from `centre_tests` to a booking.
        Index("ix_bookings_centre_test", "centre_id", "test_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    centre_id: Mapped[uuid.UUID]
    test_id: Mapped[uuid.UUID]
    appointment_at: Mapped[datetime]
    # The price that the booking copied when it was made. A later price change leaves it alone.
    amount: Mapped[Decimal]
    currency: Mapped[str]
    status: Mapped[BookingStatus] = mapped_column(
        str_enum(BookingStatus, "status"),
        default=BookingStatus.PENDING,
        server_default=BookingStatus.PENDING.value,
    )

    # The composite foreign key points at `centre_tests`, so the ORM sees no key from a booking to
    # a centre or a test. These relationships spell the join out. They only
    # read: a booking is written through its own columns.
    centre: Mapped[Centre] = relationship(
        viewonly=True,
        lazy="raise_on_sql",
        primaryjoin="Booking.centre_id == Centre.id",
        foreign_keys="Booking.centre_id",
    )
    test: Mapped[DiagnosticTest] = relationship(
        viewonly=True,
        lazy="raise_on_sql",
        primaryjoin="Booking.test_id == DiagnosticTest.id",
        foreign_keys="Booking.test_id",
    )
    payments: Mapped[list[Payment]] = relationship(
        viewonly=True,
        lazy="raise_on_sql",
        order_by="Payment.created_at, Payment.id",
    )


# The list reads one user's newest bookings first. The index also serves the `user_id` foreign key.
Index("ix_bookings_user_created", Booking.user_id, Booking.created_at.desc(), Booking.id.desc())

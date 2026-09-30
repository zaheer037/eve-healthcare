"""Centres, diagnostic tests and the price of a test at a centre (architecture §3.3).

Rows are never deleted: bookings refer to them, so `is_active` and `is_available` replace deletion.
"""

import uuid
from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, Index, UniqueConstraint, text, true
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class Centre(TimestampMixin, Base):
    __tablename__ = "centres"
    __table_args__ = (
        UniqueConstraint("name", "location", name="uq_centres_name_location"),
        # The city filter compares lower(city) on active rows, and this index serves exactly that.
        Index("ix_centres_city_active", text("lower(city)"), postgresql_where=text("is_active")),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str]
    location: Mapped[str]
    city: Mapped[str | None]
    is_active: Mapped[bool] = mapped_column(default=True, server_default=true())


class DiagnosticTest(TimestampMixin, Base):
    __tablename__ = "diagnostic_tests"
    __table_args__ = (CheckConstraint("code ~ '^[A-Z0-9_]{2,20}$'", name="code"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(unique=True)
    name: Mapped[str] = mapped_column(unique=True)
    description: Mapped[str | None]
    is_active: Mapped[bool] = mapped_column(default=True, server_default=true())


class CentreTest(TimestampMixin, Base):
    """The price of one test at one centre. Its key is the pair, so there is no `id`."""

    __tablename__ = "centre_tests"
    __table_args__ = (
        CheckConstraint("price BETWEEN 0.01 AND 99999999.99", name="price"),  # rejects NaN too
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency"),
    )

    centre_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("centres.id", ondelete="RESTRICT"), primary_key=True
    )
    test_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("diagnostic_tests.id", ondelete="RESTRICT"), primary_key=True, index=True
    )
    price: Mapped[Decimal]
    currency: Mapped[str] = mapped_column(default="INR", server_default="INR")
    is_available: Mapped[bool] = mapped_column(default=True, server_default=true())

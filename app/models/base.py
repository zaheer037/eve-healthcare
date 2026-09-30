"""The declarative base, the naming convention and the column helpers (architecture §3.2)."""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import DateTime, Enum, MetaData, Numeric, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# The convention names single-column constraints. It uses the first column only, so every
# multi-column constraint and every index gets an explicit name in its model, and a migration
# wraps that name in `op.f()`. Names stay under PostgreSQL's limit of 63 characters.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    # `Mapped[str]` is `text`, a datetime is `timestamptz` and a Decimal is `numeric(10,2)` (money).
    type_annotation_map = {  # noqa: RUF012  # SQLAlchemy's documented pattern
        str: Text,
        datetime: DateTime(timezone=True),
        Decimal: Numeric(10, 2),
    }
    # A server-side `onupdate` value comes back in the same statement (RETURNING),
    # so reading `updated_at` after a flush runs no extra query.
    __mapper_args__ = {"eager_defaults": True}  # noqa: RUF012


class TimestampMixin:
    """`created_at` and `updated_at`, last in the table. The ORM sets `updated_at` on change.

    An upsert bypasses `onupdate`, so it sets `updated_at=func.now()` itself.
    """

    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), sort_order=90)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), sort_order=91
    )


def str_enum(enum_class: type[StrEnum], name: str) -> Enum:
    """Store a StrEnum as `varchar(32)` with a named CHECK.

    A bare `Mapped[SomeStrEnum]` would create a native PostgreSQL enum type that stores member
    names. A test asserts that the schema has no native enum. Alembic never updates the CHECK,
    so a new member needs a hand-written migration.
    """
    return Enum(
        enum_class,
        native_enum=False,
        length=32,
        create_constraint=True,
        name=name,
        values_callable=lambda members: [member.value for member in members],
    )

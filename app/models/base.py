"""The declarative base and the naming convention (spec section 5.2)."""

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

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
    # A server-side `onupdate` value comes back in the same statement (RETURNING),
    # so reading `updated_at` after a flush runs no extra query.
    __mapper_args__ = {"eager_defaults": True}  # noqa: RUF012

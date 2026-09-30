"""The user table (architecture §3.3)."""

import uuid
from enum import StrEnum

from sqlalchemy import CheckConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, str_enum


class Role(StrEnum):
    PATIENT = "PATIENT"
    ADMIN = "ADMIN"


class User(TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("email = lower(email)", name="email_lowercase"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(unique=True)
    password_hash: Mapped[str]
    full_name: Mapped[str | None]
    phone: Mapped[str | None]
    role: Mapped[Role] = mapped_column(
        str_enum(Role, "role"), default=Role.PATIENT, server_default=Role.PATIENT.value
    )

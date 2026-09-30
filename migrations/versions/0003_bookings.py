"""bookings: what a user reserves at a centre

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-30 16:44:47.834011
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "bookings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("centre_id", sa.Uuid(), nullable=False),
        sa.Column("test_id", sa.Uuid(), nullable=False),
        sa.Column("appointment_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("amount", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING",
                "CONFIRMED",
                "FAILED",
                "CANCELLED",
                name="status",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            server_default="PENDING",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name=op.f("ck_bookings_currency")),
        sa.CheckConstraint("amount BETWEEN 0.01 AND 99999999.99", name=op.f("ck_bookings_amount")),
        sa.ForeignKeyConstraint(
            ["centre_id", "test_id"],
            ["centre_tests.centre_id", "centre_tests.test_id"],
            name=op.f("fk_bookings_centre_test"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_bookings_user_id"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bookings")),
    )
    op.create_index(
        op.f("ix_bookings_centre_test"), "bookings", ["centre_id", "test_id"], unique=False
    )
    op.create_index(
        op.f("ix_bookings_user_created"),
        "bookings",
        ["user_id", sa.literal_column("created_at DESC"), sa.literal_column("id DESC")],
        unique=False,
    )
    op.create_index(
        op.f("uq_bookings_active_slot"),
        "bookings",
        ["user_id", "centre_id", "test_id", "appointment_at"],
        unique=True,
        postgresql_where=sa.text("status IN ('PENDING', 'CONFIRMED')"),
    )


def downgrade() -> None:
    op.drop_index(
        op.f("uq_bookings_active_slot"),
        table_name="bookings",
        postgresql_where=sa.text("status IN ('PENDING', 'CONFIRMED')"),
    )
    op.drop_index(op.f("ix_bookings_user_created"), table_name="bookings")
    op.drop_index(op.f("ix_bookings_centre_test"), table_name="bookings")
    op.drop_table("bookings")

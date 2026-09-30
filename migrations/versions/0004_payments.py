"""payments and webhook events: what a booking is paid with and the provider's events

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-30 16:44:47.834011
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "payments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("booking_id", sa.Uuid(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING",
                "SUCCESS",
                "FAILED",
                name="status",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            server_default="PENDING",
            nullable=False,
        ),
        sa.Column("provider", sa.Text(), server_default="SIMULATOR", nullable=False),
        sa.Column("provider_payment_id", sa.Text(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint(
            "(status = 'PENDING') = (settled_at IS NULL)", name=op.f("ck_payments_settled")
        ),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name=op.f("ck_payments_currency")),
        sa.CheckConstraint("provider_payment_id <> ''", name=op.f("ck_payments_provider_ref")),
        sa.CheckConstraint("amount BETWEEN 0.01 AND 99999999.99", name=op.f("ck_payments_amount")),
        sa.ForeignKeyConstraint(
            ["booking_id"],
            ["bookings.id"],
            name=op.f("fk_payments_booking_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payments")),
    )
    op.create_index(op.f("ix_payments_booking_id"), "payments", ["booking_id"], unique=False)
    op.create_index(
        op.f("uq_payments_live_per_booking"),
        "payments",
        ["booking_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('PENDING', 'SUCCESS')"),
    )
    op.create_index(
        op.f("uq_payments_provider_ref"),
        "payments",
        ["provider", "provider_payment_id"],
        unique=True,
        postgresql_where=sa.text("provider_payment_id IS NOT NULL"),
    )
    op.create_table(
        "webhook_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column("payment_id", sa.Uuid(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "outcome",
            sa.Enum(
                "APPLIED",
                "IGNORED_NOOP",
                "NEEDS_REVIEW",
                "REJECTED",
                name="outcome",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=True,
        ),
        sa.Column("detail", sa.String(length=64), nullable=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(outcome IS NULL) = (processed_at IS NULL)", name=op.f("ck_webhook_events_processed")
        ),
        sa.ForeignKeyConstraint(
            ["payment_id"],
            ["payments.id"],
            name=op.f("fk_webhook_events_payment_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webhook_events")),
        sa.UniqueConstraint("provider", "event_id", name=op.f("uq_webhook_events_provider_event")),
    )
    op.create_index(
        op.f("ix_webhook_events_payment_id"), "webhook_events", ["payment_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_webhook_events_payment_id"), table_name="webhook_events")
    op.drop_table("webhook_events")
    op.drop_index(
        op.f("uq_payments_provider_ref"),
        table_name="payments",
        postgresql_where=sa.text("provider_payment_id IS NOT NULL"),
    )
    op.drop_index(
        op.f("uq_payments_live_per_booking"),
        table_name="payments",
        postgresql_where=sa.text("status IN ('PENDING', 'SUCCESS')"),
    )
    op.drop_index(op.f("ix_payments_booking_id"), table_name="payments")
    op.drop_table("payments")

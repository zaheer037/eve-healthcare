"""catalog: centres, diagnostic tests and the price of a test at a centre

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-30 16:12:31.905059
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "centres",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("location", sa.Text(), nullable=False),
        sa.Column("city", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_centres")),
        sa.UniqueConstraint("name", "location", name=op.f("uq_centres_name_location")),
    )
    op.create_index(
        op.f("ix_centres_city_active"),
        "centres",
        [sa.literal_column("lower(city)")],
        unique=False,
        postgresql_where=sa.text("is_active"),
    )
    op.create_table(
        "diagnostic_tests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
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
        sa.CheckConstraint("code ~ '^[A-Z0-9_]{2,20}$'", name=op.f("ck_diagnostic_tests_code")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_diagnostic_tests")),
        sa.UniqueConstraint("code", name=op.f("uq_diagnostic_tests_code")),
        sa.UniqueConstraint("name", name=op.f("uq_diagnostic_tests_name")),
    )
    op.create_table(
        "centre_tests",
        sa.Column("centre_id", sa.Uuid(), nullable=False),
        sa.Column("test_id", sa.Uuid(), nullable=False),
        sa.Column("price", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("currency", sa.Text(), server_default="INR", nullable=False),
        sa.Column("is_available", sa.Boolean(), server_default=sa.text("true"), nullable=False),
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
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name=op.f("ck_centre_tests_currency")),
        sa.CheckConstraint(
            "price BETWEEN 0.01 AND 99999999.99", name=op.f("ck_centre_tests_price")
        ),
        sa.ForeignKeyConstraint(
            ["centre_id"],
            ["centres.id"],
            name=op.f("fk_centre_tests_centre_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["test_id"],
            ["diagnostic_tests.id"],
            name=op.f("fk_centre_tests_test_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("centre_id", "test_id", name=op.f("pk_centre_tests")),
    )
    op.create_index(op.f("ix_centre_tests_test_id"), "centre_tests", ["test_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_centre_tests_test_id"), table_name="centre_tests")
    op.drop_table("centre_tests")
    op.drop_table("diagnostic_tests")
    op.drop_index(
        op.f("ix_centres_city_active"), table_name="centres", postgresql_where=sa.text("is_active")
    )
    op.drop_table("centres")

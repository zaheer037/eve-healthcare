"""Database helpers for the integration tests: create, reset, migrate and describe a schema."""

import re
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# The foreign-key index audit of the spec (section 5.6). It returns the foreign keys that no
# full index serves, so the result on a healthy schema is empty.
FK_AUDIT_SQL = """
SELECT c.conrelid::regclass::text AS table_name, c.conname AS foreign_key
FROM pg_constraint AS c
WHERE c.contype = 'f'
  AND NOT EXISTS (
        SELECT 1 FROM pg_index AS i
        WHERE i.indrelid = c.conrelid
          AND i.indisvalid
          AND i.indpred IS NULL
          AND i.indexprs IS NULL
          AND (i.indkey::int2[])[0 : cardinality(c.conkey) - 1] @> c.conkey
          AND c.conkey @> (i.indkey::int2[])[0 : cardinality(c.conkey) - 1])
"""


def _identifier(name: str | None) -> str:
    """Refuse anything but a plain lower-case name: it goes into DDL, which cannot be bound."""
    if name is None or not re.fullmatch(r"[a-z][a-z0-9_]*", name):
        raise ValueError(f"not a plain database name: {name!r}")
    return name


def _admin_engine(url: URL) -> Engine:
    return create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")


def ensure_database(url: URL) -> None:
    name = _identifier(url.database)
    admin = _admin_engine(url)
    try:
        with admin.connect() as connection:
            exists = connection.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": name}
            )
            if not exists:
                connection.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        admin.dispose()


def drop_database(url: URL) -> None:
    name = _identifier(url.database)
    admin = _admin_engine(url)
    try:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        admin.dispose()


def reset_schema(url: URL) -> None:
    """Empty the database, so a session always proves the migrations from nothing."""
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(text("DROP SCHEMA public CASCADE"))
            connection.execute(text("CREATE SCHEMA public"))
    finally:
        engine.dispose()


def alembic_config(url: URL) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["sqlalchemy_url"] = url.render_as_string(hide_password=False)
    return config


def migrate(url: URL) -> None:
    command.upgrade(alembic_config(url), "head")


_COLUMNS = """
SELECT table_name, column_name, data_type, udt_name, is_nullable, column_default,
       character_maximum_length, numeric_precision, numeric_scale
FROM information_schema.columns
WHERE table_schema = 'public' AND table_name <> 'alembic_version'
ORDER BY table_name, column_name
"""
_CONSTRAINTS = """
SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid)
FROM pg_constraint
WHERE connamespace = 'public'::regnamespace AND contype <> 'n'
  AND conrelid::regclass::text <> 'alembic_version'
ORDER BY 1, 2
"""
_INDEXES = """
SELECT tablename, indexname, indexdef
FROM pg_indexes
WHERE schemaname = 'public' AND tablename <> 'alembic_version'
ORDER BY 1, 2
"""


def describe_schema(engine: Engine) -> dict[str, list[tuple[Any, ...]]]:
    """Columns, constraints and indexes as PostgreSQL reports them. Names are part of the answer."""
    with engine.connect() as connection:
        return {
            "columns": [tuple(row) for row in connection.execute(text(_COLUMNS))],
            "constraints": [tuple(row) for row in connection.execute(text(_CONSTRAINTS))],
            "indexes": [tuple(row) for row in connection.execute(text(_INDEXES))],
        }

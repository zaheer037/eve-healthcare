"""The schema: migrations equal the models, every foreign key has an index, no native enum."""

from collections.abc import Iterator

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.engine import URL

from app.models.base import Base
from tests.database import (
    FK_AUDIT_SQL,
    alembic_config,
    describe_schema,
    drop_database,
    ensure_database,
    migrate,
)

MODELS_DATABASE = "eve_test_models"
SCRATCH_DATABASE = "eve_test_scratch"


@pytest.fixture
def models_engine(migrated_database: URL) -> Iterator[Engine]:
    """A second database built from the models alone with `create_all`."""
    url = migrated_database.set(database=MODELS_DATABASE)
    drop_database(url)
    ensure_database(url)
    engine = create_engine(url)
    try:
        Base.metadata.create_all(engine)
        yield engine
    finally:
        engine.dispose()
        drop_database(url)


def test_the_schema_from_the_migrations_equals_the_schema_from_the_models(
    db_engine: Engine, models_engine: Engine
) -> None:
    """Constraints, indexes and column types included. Any difference fails the test."""
    assert describe_schema(db_engine) == describe_schema(models_engine)


def test_alembic_check_finds_no_drift_between_the_models_and_the_migrations(
    migrated_database: URL,
) -> None:
    command.check(alembic_config(migrated_database))  # raises CommandError when a change is pending


def test_every_foreign_key_has_an_index(db_engine: Engine) -> None:
    with db_engine.connect() as connection:
        unindexed = connection.execute(text(FK_AUDIT_SQL)).all()
    assert unindexed == []


def test_the_schema_has_no_native_enum_type(db_engine: Engine) -> None:
    """A bare `Mapped[Enum]` creates a native enum that stores member names (architecture §3.2)."""
    with db_engine.connect() as connection:
        enums = connection.execute(text("SELECT typname FROM pg_type WHERE typtype = 'e'")).all()
    assert enums == []


def test_the_database_is_at_the_head_revision(db_engine: Engine, migrated_database: URL) -> None:
    head = ScriptDirectory.from_config(alembic_config(migrated_database)).get_current_head()
    with db_engine.connect() as connection:
        current = connection.scalar(text("SELECT version_num FROM alembic_version"))
    assert current == head


@pytest.fixture
def scratch_url(migrated_database: URL) -> Iterator[URL]:
    url = migrated_database.set(database=SCRATCH_DATABASE)
    drop_database(url)
    ensure_database(url)
    try:
        yield url
    finally:
        drop_database(url)


def test_every_migration_can_be_reversed(scratch_url: URL) -> None:
    """Downgrade to nothing, then upgrade again: each migration has a working rollback."""
    migrate(scratch_url)
    command.downgrade(alembic_config(scratch_url), "base")
    engine = create_engine(scratch_url)
    try:
        assert inspect(engine).get_table_names() == ["alembic_version"]
    finally:
        engine.dispose()
    migrate(scratch_url)

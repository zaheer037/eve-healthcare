"""Alembic environment: the metadata comes from the models, the URL from DATABASE_URL.

A migration needs the database URL and nothing else, so it reads `DatabaseSettings` and does
not demand the JWT and webhook secrets. The test fixture passes its own URL through
`config.attributes`, which also keeps a `%` in a password away from the ini parser.
"""

from alembic import context
from sqlalchemy import create_engine, pool

import app.models  # noqa: F401  # registers every table on Base.metadata
from app.core.config import DatabaseSettings
from app.models.base import Base

config = context.config
target_metadata = Base.metadata

# Autogenerate cannot see everything. compare_server_default and the opt-in plugin below make it
# compare defaults and named CHECK constraints (architecture §3.6). Hand-written CHECKs and partial
# indexes still go into the migration by hand.
AUTOGENERATE_PLUGINS = ["alembic.autogenerate.*", "alembic.ext.checkconstraint_byname"]


def database_url() -> str:
    url: str | None = config.attributes.get("sqlalchemy_url")
    return url or DatabaseSettings().database_url.get_secret_value()


def run_migrations_offline() -> None:
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        autogenerate_plugins=AUTOGENERATE_PLUGINS,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(database_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            autogenerate_plugins=AUTOGENERATE_PLUGINS,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

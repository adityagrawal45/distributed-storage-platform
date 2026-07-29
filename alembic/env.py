"""
Alembic environment configuration.

Design decisions:
- Migrations run synchronously (via `psycopg2`), independent of the
  app's async runtime — this is the standard, simplest, and most
  reliable approach for Alembic and avoids event-loop complications.
- `target_metadata` points at `Base.metadata`, and all models are
  imported here (via `app.models`) purely for their side effect of
  registering tables on that metadata, so `alembic revision --autogenerate`
  can detect model changes.
- The DB URL is pulled from application `Settings` (not duplicated in
  `alembic.ini`), so there is a single source of truth for connection
  configuration across the app and its migrations.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.core.config import get_settings
from app.database.session import Base
from app.models import RefreshToken, User  # noqa: F401  (registers tables on metadata)

config = context.config
settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL_SYNC)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations without a live DB connection (generates SQL only)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live DB connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

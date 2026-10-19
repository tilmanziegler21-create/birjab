"""Alembic environment. The database URL can be overridden with -x url=..."""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, pool

from app.db.models import Base

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    arguments = context.get_x_argument(as_dictionary=True)
    return arguments.get("url") or config.get_main_option("sqlalchemy.url")


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

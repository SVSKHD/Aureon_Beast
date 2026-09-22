"""Alembic environment (plan §6).

Two things here are deliberate.

**The URL is not read from ``alembic.ini``.** It comes from
``aureon.storage.postgres.database``, which is the one place that validates it and the one
place that knows how to redact it. A URL in a checked-in ini file is how a database
password ends up in a repository, and a second place that parses it is a second place that
can disagree about which database is production.

**Autogenerate compares against ``Base.metadata``**, with ``tables`` imported for its side
effect of registering all 24 of them. A missing import here would make autogenerate
cheerfully emit a migration DROPPING every table it could not see.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

from aureon.storage.postgres import tables  # noqa: F401  -- registers every table
from aureon.storage.postgres.database import database_url
from aureon.storage.postgres.migrations.env_module import render_item
from aureon.storage.postgres.models import Base

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    """The URL alembic should run against.

    ``migrate.py`` and the tests set ``sqlalchemy.url`` on the config object in-process when
    they already hold a ``Database``; otherwise it is read from the environment. Checked in
    that order so a test pointed at ``aureon_test`` cannot be overridden by a stray
    ``AUREON_DATABASE_URL`` in the shell.
    """
    configured = config.get_main_option("sqlalchemy.url", None)
    return configured or database_url()


def run_migrations_offline() -> None:
    """Emit SQL to stdout rather than running it.

    Kept working because it is how an operator reviews what a migration will do to a
    database holding real trades before it does it.
    """
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _url()
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # So a column whose TYPE drifts is reported rather than silently accepted --
            # a float that became a numeric would change what a price means.
            compare_type=True,
            render_item=render_item,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

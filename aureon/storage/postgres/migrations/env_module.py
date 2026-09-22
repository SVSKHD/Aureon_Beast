"""The autogenerate rendering hook, importable on its own.

Separate from ``env.py`` because ``env.py`` RUNS MIGRATIONS at import time -- alembic's
design -- so a test that imported it to check one function would try to migrate a database.
The hook lives here and ``env.py`` imports it (decision 347).
"""

from __future__ import annotations

from typing import Any


def render_item(type_: str, obj: Any, autogen_context: Any) -> str | bool:
    """Render ``UtcTimestamp`` as the core type whose DDL it is.

    Autogenerate's default is to spell a ``TypeDecorator`` by its Python path --
    ``aureon.storage.postgres.models.UtcTimestamp(timezone=True)`` -- which is wrong here
    twice over. It emits a name the migration never imports, so the file fails at import;
    and it couples a migration to an application class, so renaming or moving
    ``UtcTimestamp`` would break a migration that has already run in production.

    A migration describes DDL. ``UtcTimestamp``'s DDL is exactly ``TIMESTAMP WITH TIME
    ZONE``; its bind and result guards are Python-side and have nothing to do with the
    schema. So it renders as ``sa.DateTime(timezone=True)`` and the migration depends on
    SQLAlchemy alone (decision 343).
    """
    from aureon.storage.postgres.models import UtcTimestamp

    if type_ == "type" and isinstance(obj, UtcTimestamp):
        return "sa.DateTime(timezone=True)"
    return False



"""Storage backend selection.

Aureon is local-first for the current phase. SQLite is the only operational backend.
PostgreSQL remains a reserved next-step placeholder so next week's migration can happen
behind the same boundary without touching services.
"""

from __future__ import annotations

import os
from enum import StrEnum

BACKEND_ENV = "AUREON_STORAGE_BACKEND"


class StorageBackend(StrEnum):
    SQLITE = "sqlite"
    POSTGRES = "postgres"  # reserved; not enabled by runtime composition yet


DEFAULT_BACKEND = StorageBackend.SQLITE


def selected_backend(env: dict[str, str] | None = None) -> StorageBackend:
    source = os.environ if env is None else env
    raw = (source.get(BACKEND_ENV) or "").strip().lower()
    if not raw:
        return DEFAULT_BACKEND
    try:
        return StorageBackend(raw)
    except ValueError:
        known = ", ".join(sorted(backend.value for backend in StorageBackend))
        raise ValueError(
            f"{BACKEND_ENV}={raw!r} is not supported. Known values: {known}."
        ) from None


def is_postgres(env: dict[str, str] | None = None) -> bool:
    return selected_backend(env) is StorageBackend.POSTGRES


def is_sqlite(env: dict[str, str] | None = None) -> bool:
    return selected_backend(env) is StorageBackend.SQLITE


__all__ = [
    "BACKEND_ENV",
    "DEFAULT_BACKEND",
    "StorageBackend",
    "is_postgres",
    "is_sqlite",
    "selected_backend",
]

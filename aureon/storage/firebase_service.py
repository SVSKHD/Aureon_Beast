"""Removed Firebase compatibility tombstone.

Aureon no longer constructs or talks to Firebase/Firestore. Runtime storage is local
SQLite. This module remains temporarily only so an old import fails with an explicit
migration message instead of an opaque ImportError; it contains no Firebase SDK import
and performs no network operation.
"""

from __future__ import annotations

from typing import Any


class FirebaseRemovedError(RuntimeError):
    """Raised when legacy code still tries to construct a Firebase client."""


def get_client(*, project_id: str | None = None, emulator_host: str | None = None) -> Any:
    raise FirebaseRemovedError(
        "Firebase/Firestore was removed from Aureon runtime. "
        "Use aureon.storage.runtime.build_storage() for local SQLite storage."
    )


def set_client(client: Any | None) -> None:
    if client is not None:
        raise FirebaseRemovedError(
            "Firebase client injection is no longer supported; inject repositories or "
            "a local StorageRuntime instead."
        )


def reset_client() -> None:
    return None


__all__ = ["FirebaseRemovedError", "get_client", "reset_client", "set_client"]

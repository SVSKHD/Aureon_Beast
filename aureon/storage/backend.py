"""Which storage backend this process uses (plan §3, §4).

One environment variable, read in one place, reported by preflight (§35). The seam exists
so the migration is a switch rather than a rewrite: every service acquires storage through
a factory that consults this, so S-4 changes what gets constructed and nothing else.

**The default is still Firestore, and that is deliberate for exactly this one step.** S-1
adds the PostgreSQL package beside the working system; S-4 is the commit that switches the
services over and deletes the Firestore client code. Flipping the default here before the
repositories exist would mean a commit in which the system does not run -- and "every
phase ends with its acceptance test green" (CLAUDE.md) does not admit an intermediate
state where it does not. The test below pins the default, so the flip is a visible, single
line in the commit that earns it rather than something that drifted.
"""

from __future__ import annotations

import os
from enum import StrEnum

BACKEND_ENV = "AUREON_STORAGE_BACKEND"


class StorageBackend(StrEnum):
    """The backends this codebase knows how to construct.

    A closed set rather than a free string, because the failure mode of a typo is the worst
    one available: ``AUREON_STORAGE_BACKEND=postgress`` falling back to a default would
    start the system against the *other* database and report success. So an unrecognised
    value is a refusal to start.
    """

    #: Removed in S-4, once every service reads PostgreSQL and the client code is gone.
    FIRESTORE = "firestore"
    #: Local PostgreSQL: the application truth from Phase 13 onward (plan §2).
    POSTGRES = "postgres"


#: Flipped to ``POSTGRES`` by S-4. See the module docstring for why not yet.
DEFAULT_BACKEND = StorageBackend.FIRESTORE


def selected_backend(env: dict[str, str] | None = None) -> StorageBackend:
    """``AUREON_STORAGE_BACKEND``, or the default.

    Takes the environment as an argument so the validation is testable without mutating
    the process -- the same shape as ``database.database_url``.
    """
    source = os.environ if env is None else env
    raw = (source.get(BACKEND_ENV) or "").strip().lower()
    if not raw:
        return DEFAULT_BACKEND
    try:
        return StorageBackend(raw)
    except ValueError:
        known = ", ".join(sorted(backend.value for backend in StorageBackend))
        raise ValueError(
            f"{BACKEND_ENV}={raw!r} is not a storage backend this build knows. "
            f"Known backends: {known}. Refusing to fall back to a default, because "
            "starting against the wrong database and reporting success is worse than "
            "not starting."
        ) from None


def is_postgres(env: dict[str, str] | None = None) -> bool:
    """Whether this process should use local PostgreSQL."""
    return selected_backend(env) is StorageBackend.POSTGRES


__all__ = ["BACKEND_ENV", "DEFAULT_BACKEND", "StorageBackend", "is_postgres", "selected_backend"]

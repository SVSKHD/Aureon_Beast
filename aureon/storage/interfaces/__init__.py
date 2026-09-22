"""What a service is allowed to assume about storage (plan §4).

The seam: ``Service → Repository interface → Storage backend → PostgreSQL``. A service
depends on the protocol, not on the class, so S-4's switch is a change of what gets
constructed in ``main_*.py`` and not a change to any service.

Protocols rather than abstract base classes, on purpose. An ABC would require every
repository to inherit from something in this package, which means the inheritance is the
thing a reviewer checks and the *shape* is not; a Protocol is checked structurally, so an
implementation that drifted from the interface fails type-checking at the call site where
it matters. It also means the existing Firestore repositories satisfy the interfaces
without being edited, which is what makes the migration a substitution rather than a
rewrite.

Each domain's protocol lands with its repository in S-3. What is here now is the
cross-cutting vocabulary those protocols are written in.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Transactional(Protocol):
    """A storage backend that can open an atomic unit of work.

    The one capability the money path requires of storage and cannot work around. §12's
    setup write, §15's executor claim and §17's control claim are each "a transaction
    containing a locking read and the writes that follow from it"; a backend that cannot
    offer that boundary cannot hold ``trade_requests``, whatever else it can do.

    Declared here rather than taken as "the concrete ``Database``" so the fail-closed
    tests in S-5 can pass a backend that refuses to open one.
    """

    def transaction(self) -> Any:
        """A context manager that commits on success and rolls back on any exception."""
        ...

    def connect(self) -> Any:
        """A context manager for a read with no transaction opened."""
        ...


@runtime_checkable
class Probeable(Protocol):
    """A backend that can be asked whether it is there.

    Split from ``Transactional`` because preflight (§35) needs to *report* on a backend it
    may not be able to use, and a health check that had to open a transaction to answer
    would fail for a reason it could not distinguish from the thing it was checking.
    """

    def probe(self) -> None:
        """Raise if the backend does not answer. Return ``None`` if it does."""
        ...


@runtime_checkable
class Readable(Protocol):
    """A repository that only reads.

    Discord and the reviews hold these. §71 and the boundary tests already say Discord may
    not import a writable observation repository; a reader protocol is how the *type* says
    the same thing, so the guard and the signature agree instead of the guard standing
    alone.
    """

    def get(self, identifier: str) -> Any | None: ...


def iter_none() -> Iterator[Any]:
    """An empty iterator, for a reader with nothing to return.

    Named rather than written as ``iter(())`` at each site so a caller that means "the
    backend is unavailable" cannot express it as an empty result by accident -- §27 wants
    that distinction to be impossible to blur.
    """
    return iter(())


__all__ = ["Probeable", "Readable", "Transactional", "iter_none"]

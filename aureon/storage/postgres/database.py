"""The single PostgreSQL engine factory and transaction boundary (plan §4, §5).

The only place in the system that opens a database connection. Every repository takes a
``Database`` and asks it for a connection or a transaction, which is what lets a test
point the whole storage layer at ``aureon_test`` by constructing one object.

Three things here are load-bearing rather than plumbing:

**The URL is validated, not trusted.** ``AUREON_DATABASE_URL`` must name the
``postgresql+psycopg`` driver and a database. A URL missing its database name connects to
the one named after the role -- so a typo would silently open a *different*, probably
empty, database and every read would return "no data yet". That is the same failure mode
the collection prefix guarded against, and it deserves the same refusal.

**The password is never logged.** Every message about the connection goes through
``redacted``, which is a function rather than a convention because a convention is one
``log.info(url)`` away from putting a password in a log file somebody pastes into an
issue.

**The startup wait is here, not in the launcher (C-4).** PostgreSQL runs as a Windows
service and the machine may still be starting it when Aureon starts. So a connection
failure at startup is retried for ``AUREON_DB_STARTUP_WAIT_SECONDS`` before preflight is
allowed to fail -- and preflight and ``main_aureon.py`` share this one implementation, so
they cannot disagree about how long "still starting" lasts.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlsplit, urlunsplit

log = logging.getLogger(__name__)

#: The driver the whole system is built on. psycopg 3, named explicitly in the URL rather
#: than left to SQLAlchemy's default -- which is psycopg2, a different library with
#: different behaviour around server-side cursors and ``LISTEN``.
DRIVER = "postgresql+psycopg"

URL_ENV = "AUREON_DATABASE_URL"

#: C-4. How long a connection failure at startup is treated as "the service is still
#: coming up" rather than as an outage.
STARTUP_WAIT_ENV = "AUREON_DB_STARTUP_WAIT_SECONDS"
DEFAULT_STARTUP_WAIT_SECONDS = 120.0

#: The delay between startup attempts. Fixed rather than exponential: the question is
#: "has the service finished starting", which is answered by asking again shortly, and an
#: exponential backoff would spend most of a 120-second budget asleep.
STARTUP_RETRY_SECONDS = 2.0

#: Kept small on purpose. The money path is one executor holding one claim at a time, and
#: the observer's writes are a handful per candle; a large pool would mostly be idle
#: sockets against a server sharing a laptop with MetaTrader 5.
DEFAULT_POOL_SIZE = 5
DEFAULT_MAX_OVERFLOW = 5

#: Recycle before the server's own idle timeout can close a socket underneath us, which
#: presents as an inscrutable "server closed the connection unexpectedly" on the first
#: query after a quiet market period.
DEFAULT_POOL_RECYCLE_SECONDS = 1800


class DatabaseUrlError(ValueError):
    """``AUREON_DATABASE_URL`` is missing, malformed, or names the wrong driver."""


class DatabaseUnavailable(RuntimeError):
    """The local database could not be reached.

    Raised rather than returned so no caller can treat an outage as an empty result. §27:
    on the money path this must fail closed, and a function that returned ``None`` here
    would be indistinguishable from "no confirmed requests".
    """


# ── The URL ───────────────────────────────────────────────────────────────────


def database_url(env: dict[str, str] | None = None) -> str:
    """``AUREON_DATABASE_URL``, validated.

    Takes the environment as an argument so a test can check the validation without
    mutating the process, and reads ``os.environ`` when not given one.
    """
    source = os.environ if env is None else env
    # Stripped before the emptiness check so ``AUREON_DATABASE_URL=`` in a ``.env`` file --
    # set, and whitespace -- gets the "is not set" diagnostic below rather than falling
    # through to ``validated_url`` and being reported as a driver problem. Both refuse; only
    # one of them tells the operator what is actually wrong (decision 337).
    raw = (source.get(URL_ENV) or "").strip()
    if not raw:
        raise DatabaseUrlError(
            f"{URL_ENV} is not set. Expected "
            f"{DRIVER}://aureon:<password>@127.0.0.1:5432/aureon (plan §3)."
        )
    return validated_url(raw)


def validated_url(raw: str) -> str:
    """Reject a URL that would connect somewhere other than where it appears to."""
    parts = urlsplit(raw)
    if parts.scheme != DRIVER:
        raise DatabaseUrlError(
            f"{URL_ENV} must use the {DRIVER!r} driver, not {parts.scheme!r}. "
            "psycopg2 and the bare 'postgresql' scheme are a different library."
        )
    if not parts.hostname:
        raise DatabaseUrlError(f"{URL_ENV} has no host: {redacted(raw)}")
    if not parts.path.lstrip("/"):
        raise DatabaseUrlError(
            f"{URL_ENV} names no database: {redacted(raw)}. A URL without one connects "
            "to the database named after the role, which reads as an empty deployment."
        )
    if "/" in parts.path.lstrip("/"):
        raise DatabaseUrlError(
            f"{URL_ENV} path must be a single database name: {redacted(raw)}"
        )
    return raw


def database_name(url: str) -> str:
    """The database a URL points at. The input to C-9's test-isolation refusal."""
    return urlsplit(url).path.lstrip("/")


def redacted(url: str) -> str:
    """The URL with its password replaced, for logs and error messages.

    Returns the input unchanged if it cannot be parsed: a redactor that raised would turn
    a bad-URL diagnostic into a stack trace about the diagnostic.
    """
    try:
        parts = urlsplit(url)
    except ValueError:  # pragma: no cover - urlsplit is forgiving
        return "<unparseable database url>"
    if not parts.password:
        return url
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    netloc = f"{parts.username or ''}:***@{host}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def startup_wait_seconds(env: dict[str, str] | None = None) -> float:
    """``AUREON_DB_STARTUP_WAIT_SECONDS``, defaulting to 120 (C-4).

    A value that does not parse falls back to the default with a warning rather than
    refusing to start: the consequence of a typo here should be a normal startup, not a
    system that will not run because a *timeout* was spelled wrong.
    """
    source = os.environ if env is None else env
    raw = (source.get(STARTUP_WAIT_ENV) or "").strip()
    if not raw:
        return DEFAULT_STARTUP_WAIT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        log.warning(
            "%s=%r is not a number; using the default of %.0fs",
            STARTUP_WAIT_ENV,
            raw,
            DEFAULT_STARTUP_WAIT_SECONDS,
        )
        return DEFAULT_STARTUP_WAIT_SECONDS
    if value < 0:
        log.warning("%s=%r is negative; using 0", STARTUP_WAIT_ENV, raw)
        return 0.0
    return value


# ── The database ──────────────────────────────────────────────────────────────


class Database:
    """One engine, and the transaction boundary every repository writes through.

    The engine is built lazily on first use so constructing a ``Database`` cannot fail
    and cannot open a socket. That matters for preflight, which wants to *report* on the
    connection rather than die while being set up, and for the tests, which construct one
    per module.
    """

    def __init__(
        self,
        url: str,
        *,
        echo: bool = False,
        pool_size: int = DEFAULT_POOL_SIZE,
        max_overflow: int = DEFAULT_MAX_OVERFLOW,
        pool_recycle: int = DEFAULT_POOL_RECYCLE_SECONDS,
        engine_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._url = validated_url(url)
        self._echo = echo
        self._pool_size = pool_size
        self._max_overflow = max_overflow
        self._pool_recycle = pool_recycle
        self._engine_factory = engine_factory
        self._engine: Any | None = None
        self._lock = threading.Lock()

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def url(self) -> str:
        return self._url

    @property
    def name(self) -> str:
        """The database name, for the preflight row and C-9's refusal."""
        return database_name(self._url)

    def __repr__(self) -> str:
        """Redacted, so a repr in a traceback cannot leak the password."""
        return f"Database({redacted(self._url)})"

    # ── The engine ────────────────────────────────────────────────────────────

    @property
    def engine(self) -> Any:
        """The process-wide engine for this URL, built once."""
        with self._lock:
            if self._engine is None:
                self._engine = self._build_engine()
            return self._engine

    def _build_engine(self) -> Any:
        factory = self._engine_factory
        if factory is None:
            from sqlalchemy import create_engine

            factory = create_engine
        log.info("opening the local database %s", redacted(self._url))
        return factory(
            self._url,
            echo=self._echo,
            pool_size=self._pool_size,
            max_overflow=self._max_overflow,
            pool_recycle=self._pool_recycle,
            # Verify the socket before handing it out. Without this a connection killed
            # while the market was closed surfaces as a failed query on the first
            # detection of the next session -- at which point it looks like a bug in the
            # observer rather than a socket that had gone stale.
            pool_pre_ping=True,
            future=True,
        )

    def dispose(self) -> None:
        """Close every pooled connection. Used at shutdown and between test modules."""
        with self._lock:
            engine, self._engine = self._engine, None
        if engine is not None:
            engine.dispose()

    # ── Connections and transactions ──────────────────────────────────────────

    @contextmanager
    def connect(self) -> Iterator[Any]:
        """A connection with no transaction opened. Reads only.

        Wrapped so every failure to reach the server arrives as ``DatabaseUnavailable``:
        callers on the money path have to distinguish "the database said no rows" from
        "the database did not answer", and SQLAlchemy's ``OperationalError`` is easy to
        catch alongside a query error by accident.
        """
        try:
            with self.engine.connect() as connection:
                yield connection
        except Exception as exc:
            raise _as_unavailable(exc, self._url) from exc

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        """``BEGIN`` … ``COMMIT``, rolling back on any exception.

        The unit of atomicity for every state change in the system. §12's setup write and
        §15's executor claim are both "one of these, containing a ``SELECT … FOR UPDATE``
        and the writes that follow from it" -- so the rollback has to be unconditional and
        it has to cover an exception raised by the *caller's* body, not only by a query.
        """
        try:
            with self.engine.begin() as connection:
                yield connection
        except Exception as exc:
            raise _as_unavailable(exc, self._url) from exc

    # ── Health ────────────────────────────────────────────────────────────────

    def probe(self) -> None:
        """``SELECT 1``. Raises ``DatabaseUnavailable`` if the server does not answer."""
        from sqlalchemy import text

        with self.connect() as connection:
            connection.execute(text("SELECT 1"))

    def transaction_probe(self) -> None:
        """Open a transaction, write nothing, commit (§35's ``transaction_probe`` row).

        Separate from ``probe`` because they fail for different reasons: a read-only
        replica, a database in recovery, or a role without write permission all answer
        ``SELECT 1`` perfectly and then refuse the first transaction. An operator told
        "connection PASS" and then watched the executor fail would have no way to see why.
        """
        from sqlalchemy import text

        with self.transaction() as connection:
            connection.execute(text("SELECT 1"))

    def wait_until_ready(
        self,
        *,
        seconds: float | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.monotonic,
    ) -> float:
        """Retry ``probe`` until it succeeds or the budget runs out (C-4).

        Returns how long the wait took, so a startup banner can say "the database took
        18s to come up" -- which is the difference between a slow service and a healthy
        one, and the only warning an operator gets before the wait stops being enough.

        ``sleep`` and ``now`` are injected so the tests exercise a 120-second budget
        without taking 120 seconds.
        """
        budget = startup_wait_seconds() if seconds is None else seconds
        started = now()
        attempts = 0
        while True:
            attempts += 1
            try:
                self.probe()
            except DatabaseUnavailable as exc:
                waited = now() - started
                if waited + STARTUP_RETRY_SECONDS > budget:
                    raise DatabaseUnavailable(
                        f"{redacted(self._url)} did not answer within {budget:.0f}s "
                        f"({attempts} attempts). Is the PostgreSQL service running?"
                    ) from exc
                log.info(
                    "the database is not up yet (%.0fs of %.0fs); retrying",
                    waited,
                    budget,
                )
                # No dispose between attempts, and that was checked rather than assumed:
                # SQLAlchemy's pool does not retain a connection that failed to open, so a
                # later attempt builds a fresh one and sees the service come up. An earlier
                # version disposed here and justified it with a claim about a cached dead
                # socket that turned out to be false (decision 336).
                sleep(STARTUP_RETRY_SECONDS)
                continue
            return now() - started


def _as_unavailable(exc: Exception, url: str) -> Exception:
    """Wrap a connection failure, and let a real query error through unchanged.

    A ``ProgrammingError`` on a misspelled column is a bug in Aureon and must keep its own
    type and message; an ``OperationalError`` reaching the server is an outage and must
    become ``DatabaseUnavailable`` so §27's fail-closed path can catch one thing.
    """
    if isinstance(exc, DatabaseUnavailable):
        return exc
    try:
        from sqlalchemy.exc import InterfaceError, OperationalError
    except ImportError:  # pragma: no cover - sqlalchemy is a core dependency
        return exc
    if isinstance(exc, (OperationalError, InterfaceError)):
        return DatabaseUnavailable(f"{redacted(url)} is not reachable: {exc}")
    return exc


# ── The process-wide instance ─────────────────────────────────────────────────
# Mirrors ``firebase_service``'s shape deliberately: the services acquire their storage
# through a module-level getter with a setter for tests, so S-4's switch is a change of
# which module that getter lives in rather than a change to every service.

_database: Database | None = None
_database_lock = threading.Lock()


def get_database() -> Database:
    """The process-wide ``Database``, built from the environment on first call."""
    global _database
    with _database_lock:
        if _database is None:
            _database = Database(database_url())
        return _database


def set_database(database: Database | None) -> None:
    """Install a ``Database`` explicitly. Used by tests and by the launcher."""
    global _database
    with _database_lock:
        _database = database


def reset_database() -> None:
    """Forget the cached instance, disposing its pool first."""
    global _database
    with _database_lock:
        existing, _database = _database, None
    if existing is not None:
        existing.dispose()

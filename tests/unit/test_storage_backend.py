"""The storage seam: which backend, which database, and how long to wait for it.

Everything here is decided without a server, so it runs everywhere. The tests that need a
real PostgreSQL live in ``tests/postgres/`` and skip when none is configured (C-10) -- the
split exists so a machine with no database still exercises the logic most likely to be
wrong, which is the parsing and the refusals rather than the SQL.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy.orm import Mapped, mapped_column

from aureon.storage.backend import (
    BACKEND_ENV,
    DEFAULT_BACKEND,
    StorageBackend,
    is_postgres,
    is_sqlite,
    selected_backend,
)
from aureon.storage.postgres.database import (
    DEFAULT_STARTUP_WAIT_SECONDS,
    DRIVER,
    STARTUP_RETRY_SECONDS,
    Database,
    DatabaseUnavailable,
    DatabaseUrlError,
    database_name,
    database_url,
    redacted,
    startup_wait_seconds,
    validated_url,
)
from tests.postgres_support import (
    PRODUCTION_DATABASE_NAME,
    TEST_DATABASE_NAME,
    UnsafeTestDatabase,
    admin_url,
    assert_test_database,
    drop_test_database,
    guard_configured_url,
    resolve_test_url,
    should_drop,
    with_database,
)

GOOD = f"{DRIVER}://aureon:secret@127.0.0.1:5432/aureon"
GOOD_TEST = f"{DRIVER}://aureon:secret@127.0.0.1:5432/{TEST_DATABASE_NAME}"


# ── The backend selector ──────────────────────────────────────────────────────


def test_the_default_backend_is_local_sqlite() -> None:
    assert DEFAULT_BACKEND is StorageBackend.SQLITE
    assert selected_backend({}) is StorageBackend.SQLITE
    assert is_sqlite({}) is True
    assert is_postgres({}) is False


def test_an_explicit_postgres_setting_is_honoured() -> None:
    assert selected_backend({BACKEND_ENV: "postgres"}) is StorageBackend.POSTGRES
    assert is_postgres({BACKEND_ENV: "postgres"}) is True


def test_case_and_whitespace_do_not_change_the_backend() -> None:
    """``.env`` files acquire trailing spaces and capital letters; neither is a typo."""
    assert selected_backend({BACKEND_ENV: "  POSTGRES "}) is StorageBackend.POSTGRES


def test_an_unknown_backend_refuses_to_start() -> None:
    """A typo must not fall back to a default.

    ``AUREON_STORAGE_BACKEND=postgress`` silently defaulting would start the system
    against the OTHER database and report success -- the money path reading one store while
    the observer wrote another, both healthy.
    """
    with pytest.raises(ValueError) as raised:
        selected_backend({BACKEND_ENV: "postgress"})
    message = str(raised.value)
    assert "postgress" in message
    assert "postgres" in message and "sqlite" in message, "the message must list the known set"


def test_every_backend_is_reachable_through_the_environment() -> None:
    """A member nobody can select would be a branch nobody can reach."""
    for backend in StorageBackend:
        assert selected_backend({BACKEND_ENV: backend.value}) is backend


# ── The database URL ──────────────────────────────────────────────────────────


def test_a_valid_url_passes_through_unchanged() -> None:
    assert validated_url(GOOD) == GOOD
    assert database_name(GOOD) == "aureon"


def test_the_psycopg2_driver_is_refused() -> None:
    """psycopg2 is a different library, and the difference matters for ``LISTEN`` (C-8).

    SQLAlchemy's bare ``postgresql://`` means psycopg2, so a URL copied from a tutorial
    would quietly select a driver this system was not built on.
    """
    for scheme in ("postgresql", "postgresql+psycopg2", "postgres"):
        with pytest.raises(DatabaseUrlError) as raised:
            validated_url(f"{scheme}://aureon:secret@127.0.0.1:5432/aureon")
        assert DRIVER in str(raised.value)


def test_a_url_with_no_database_is_refused() -> None:
    """Without a database name libpq connects to the one named after the role.

    So the connection succeeds, the tables are absent, and every read returns "no data
    yet" -- indistinguishable from a fresh deployment.
    """
    with pytest.raises(DatabaseUrlError) as raised:
        validated_url(f"{DRIVER}://aureon:secret@127.0.0.1:5432/")
    assert "names no database" in str(raised.value)


def test_a_url_with_no_host_is_refused() -> None:
    with pytest.raises(DatabaseUrlError):
        validated_url(f"{DRIVER}:///aureon")


def test_a_missing_environment_variable_names_the_expected_shape() -> None:
    """The error is the only documentation an operator reads at 3am."""
    with pytest.raises(DatabaseUrlError) as raised:
        database_url({})
    message = str(raised.value)
    assert "AUREON_DATABASE_URL" in message
    assert DRIVER in message


def test_a_blank_environment_variable_is_treated_as_missing() -> None:
    """``AUREON_DATABASE_URL=`` in a ``.env`` file is a variable that is set and empty.

    The assertion is on the MESSAGE, because both paths refuse. Without the strip, a
    whitespace value reaches ``validated_url`` and is reported as a driver problem -- true,
    useless, and it sends an operator to look at a driver when the variable is simply blank.
    A plant removing the strip survived a test that only checked that something was raised.
    """
    with pytest.raises(DatabaseUrlError) as raised:
        database_url({"AUREON_DATABASE_URL": "   "})
    message = str(raised.value)
    assert "is not set" in message, f"a blank variable was diagnosed as something else: {message}"
    assert "driver" not in message


# ── The password never reaches a log ──────────────────────────────────────────


def test_the_password_is_redacted() -> None:
    assert "secret" not in redacted(GOOD)
    assert redacted(GOOD) == f"{DRIVER}://aureon:***@127.0.0.1:5432/aureon"


def test_redaction_keeps_everything_an_operator_needs() -> None:
    """A redaction that hid the host would make the diagnostic useless."""
    shown = redacted(GOOD)
    assert "aureon@" not in shown  # the user is kept, but not glued to the host
    for keep in ("aureon", "127.0.0.1", "5432", "/aureon"):
        assert keep in shown


def test_a_url_without_a_password_is_unchanged() -> None:
    """Peer authentication and a ``.pgpass`` file both produce one."""
    bare = f"{DRIVER}://aureon@127.0.0.1:5432/aureon"
    assert redacted(bare) == bare


def test_the_repr_does_not_leak_the_password() -> None:
    """A ``Database`` in a traceback or a pytest diff prints its repr."""
    assert "secret" not in repr(Database(GOOD))


def test_an_unreachable_error_message_is_redacted() -> None:
    """§27's exception travels into logs and ops events; it carries the URL."""
    database = Database(f"{DRIVER}://aureon:secret@127.0.0.1:1/aureon_test")
    try:
        with pytest.raises(DatabaseUnavailable) as raised:
            database.probe()
        assert "secret" not in str(raised.value)
    finally:
        database.dispose()


# ── The startup wait (C-4) ────────────────────────────────────────────────────


def test_the_startup_wait_defaults_to_two_minutes() -> None:
    assert startup_wait_seconds({}) == DEFAULT_STARTUP_WAIT_SECONDS == 120.0


def test_the_startup_wait_is_configurable() -> None:
    assert startup_wait_seconds({"AUREON_DB_STARTUP_WAIT_SECONDS": "45"}) == 45.0


def test_an_unparseable_startup_wait_falls_back_rather_than_refusing() -> None:
    """A typo in a TIMEOUT must not be the reason the system will not start."""
    assert (
        startup_wait_seconds({"AUREON_DB_STARTUP_WAIT_SECONDS": "two minutes"})
        == DEFAULT_STARTUP_WAIT_SECONDS
    )


def test_a_negative_startup_wait_becomes_zero() -> None:
    assert startup_wait_seconds({"AUREON_DB_STARTUP_WAIT_SECONDS": "-5"}) == 0.0


class _Clock:
    """A monotonic clock that only moves when something sleeps.

    So a 120-second budget is exercised in no time at all, and the test asserts the
    RETRY POLICY rather than the wall clock.
    """

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def time(self) -> float:
        return self.now


def _database_that_fails(times: int) -> tuple[Database, list[int]]:
    """A ``Database`` whose probe fails ``times`` times and then succeeds."""
    database = Database(GOOD_TEST)
    attempts: list[int] = []

    def probe() -> None:
        attempts.append(len(attempts))
        if len(attempts) <= times:
            raise DatabaseUnavailable("still starting")

    database.probe = probe  # type: ignore[method-assign]
    return database, attempts


def test_a_service_that_is_still_starting_is_waited_for() -> None:
    """C-4: PostgreSQL is a Windows service and the machine may still be starting it."""
    clock = _Clock()
    database, attempts = _database_that_fails(3)
    waited = database.wait_until_ready(seconds=120, sleep=clock.sleep, now=clock.time)
    assert len(attempts) == 4, "it stopped retrying before the budget ran out"
    assert waited == pytest.approx(3 * STARTUP_RETRY_SECONDS)


def test_a_server_that_never_comes_up_raises_after_the_budget() -> None:
    """And the message says what to check, because the next step is an operator's."""
    clock = _Clock()
    database, attempts = _database_that_fails(10_000)
    with pytest.raises(DatabaseUnavailable) as raised:
        database.wait_until_ready(seconds=10, sleep=clock.sleep, now=clock.time)
    message = str(raised.value)
    assert "PostgreSQL service" in message
    assert "secret" not in message
    assert len(attempts) >= 2, "it gave up without retrying at all"


def test_a_zero_budget_still_tries_once() -> None:
    """A wait of zero means "do not wait", not "do not look".

    An operator who sets this to 0 wants preflight to fail immediately when the database is
    down -- not to skip the check and pass.
    """
    clock = _Clock()
    database, attempts = _database_that_fails(0)
    database.wait_until_ready(seconds=0, sleep=clock.sleep, now=clock.time)
    assert len(attempts) == 1
    assert clock.slept == []


def test_the_wait_returns_immediately_when_the_database_is_up() -> None:
    clock = _Clock()
    database, attempts = _database_that_fails(0)
    assert database.wait_until_ready(seconds=120, sleep=clock.sleep, now=clock.time) == 0.0
    assert clock.slept == []


# ── C-9: the suite cannot be pointed at production ────────────────────────────


def test_the_production_database_is_refused_by_name() -> None:
    """The refusal this correction exists for, with its own message.

    The suite creates, truncates and drops tables. Pointed at ``aureon`` it would do that
    to the only copy of every detection, evaluation and trade the system has.
    """
    with pytest.raises(UnsafeTestDatabase) as raised:
        assert_test_database(GOOD)
    message = str(raised.value)
    assert PRODUCTION_DATABASE_NAME in message
    assert "PRODUCTION" in message
    assert "secret" not in message, "the refusal must not print the password"


def test_any_database_but_the_test_one_is_refused() -> None:
    """The broader half of C-9. ``aureon_staging`` is not production and is still not ours."""
    for name in ("aureon_staging", "postgres", "scratch", "aureon_test_2"):
        with pytest.raises(UnsafeTestDatabase):
            assert_test_database(with_database(GOOD, name))


def test_the_test_database_is_accepted_and_returned() -> None:
    assert assert_test_database(GOOD_TEST) == GOOD_TEST


def test_the_guard_runs_on_the_real_environment_at_import() -> None:
    """This module imported at all means the guard in ``tests/conftest.py`` let it.

    Re-asserted here so the guard has a test of its own rather than only a side effect: a
    conftest edit that dropped the call would leave every test passing.
    """
    guard_configured_url()  # the live environment; raises if it is unsafe
    with pytest.raises(UnsafeTestDatabase):
        guard_configured_url({"AUREON_DATABASE_URL": GOOD})


def test_an_absent_url_is_not_an_error() -> None:
    """Most of the suite needs no database, and must not require one to be configured."""
    guard_configured_url({})
    guard_configured_url({"AUREON_DATABASE_URL": "  "})


def test_a_configured_url_cannot_route_around_the_refusal() -> None:
    """``resolve_test_url`` re-checks rather than trusting the import-time guard.

    A test that builds its own environment dict would otherwise bypass it entirely.
    """
    with pytest.raises(UnsafeTestDatabase):
        resolve_test_url({"AUREON_DATABASE_URL": GOOD})


# ── C-10: how the suite finds a server ────────────────────────────────────────


def test_an_admin_url_yields_the_test_database() -> None:
    """The no-Docker path: a role that may CREATE DATABASE on the local server."""
    url, reason = resolve_test_url(
        {"AUREON_TEST_ADMIN_URL": f"{DRIVER}://aureon:secret@127.0.0.1:5432/postgres"}
    )
    assert reason is None
    assert url is not None and database_name(url) == TEST_DATABASE_NAME


def test_a_configured_url_wins_over_the_admin_url() -> None:
    """CI exports the service container's URL; nothing should then create a database."""
    url, reason = resolve_test_url(
        {
            "AUREON_DATABASE_URL": GOOD_TEST,
            "AUREON_TEST_ADMIN_URL": f"{DRIVER}://other:x@10.0.0.1:5432/postgres",
        }
    )
    assert reason is None
    assert url == GOOD_TEST


def test_with_no_configuration_the_reason_names_both_variables() -> None:
    """A skip that does not say how to un-skip is a skip nobody ever resolves."""
    url, reason = resolve_test_url({})
    assert url is None
    assert reason is not None
    assert "AUREON_DATABASE_URL" in reason and "AUREON_TEST_ADMIN_URL" in reason


def test_the_admin_url_is_read_from_its_own_variable() -> None:
    assert admin_url({}) is None
    assert admin_url({"AUREON_TEST_ADMIN_URL": " x "}) == "x"


def test_dropping_anything_but_a_test_database_is_refused() -> None:
    """The cleanup helper is the one place a DROP DATABASE is written.

    A mistyped argument there would be the single most expensive typo in the repository, so
    the name is checked before a connection is even opened -- which is why this test needs
    no server.
    """
    admin = f"{DRIVER}://aureon:secret@127.0.0.1:5432/postgres"
    for name in (PRODUCTION_DATABASE_NAME, "", "postgres", "aureon_prod"):
        with pytest.raises(UnsafeTestDatabase):
            drop_test_database(admin, name=name)


def test_the_harness_drops_only_a_database_it_created() -> None:
    """The rule that protects a database the suite was merely pointed at.

    CI hands us a service container and a developer may hand us their own; either way
    ``created`` is false and the database must survive the run. Without this the first time
    somebody exported ``AUREON_DATABASE_URL`` to debug one test, they would lose it -- and
    would find out by losing it, which is the worst way to learn a harness rule.
    """
    admin = f"{DRIVER}://aureon:secret@127.0.0.1:5432/postgres"
    assert should_drop(created=True, admin=admin) is True
    assert should_drop(created=False, admin=admin) is False, "it dropped a database it was given"
    assert should_drop(created=True, admin=None) is False
    assert should_drop(created=False, admin=None) is False


def test_a_mapped_datetime_gets_the_strict_timestamp_type() -> None:
    """``Base.type_annotation_map`` is the seam every S-2 table inherits the guard through.

    Nothing exercises it yet -- the tests above build tables with an explicit
    ``UtcTimestamp`` column, and the real tables arrive in S-2 -- so a plant swapping the map
    to a plain ``DateTime`` survived everything. That is the shape of the failure this test
    prevents: the guard would still exist, and no table would be using it.
    """
    from aureon.storage.postgres.models import Base, UtcTimestamp

    # ``Mapped`` and ``datetime`` are imported at MODULE level on purpose: this file has
    # ``from __future__ import annotations``, so SQLAlchemy resolves ``Mapped[datetime]``
    # from the module namespace and a function-local import is invisible to it.
    class _Probe(Base):
        __tablename__ = "s1_annotation_probe"
        id: Mapped[int] = mapped_column(primary_key=True)
        at: Mapped[datetime] = mapped_column()
        maybe: Mapped[datetime | None] = mapped_column()

    try:
        assert isinstance(_Probe.__table__.c.at.type, UtcTimestamp)
        assert isinstance(_Probe.__table__.c.maybe.type, UtcTimestamp), (
            "an OPTIONAL datetime skipped the guard, which is where a nullable "
            "claimed_at or closed_at would have got in"
        )
    finally:
        # The base's metadata is process-wide; leaving this table in it would make S-2's
        # generated contracts file list a test probe.
        Base.metadata.remove(_Probe.__table__)


def test_the_conftest_actually_applies_the_refusal() -> None:
    """C-9's wiring, checked structurally rather than by calling the function myself.

    The first version of this called ``guard_configured_url()`` here and asserted it raised
    on a bad URL -- which tests the FUNCTION. A plant commenting out the call in
    ``tests/conftest.py`` survived it, because the function was still perfectly correct and
    nobody was calling it. The same mistake as decision 317's annotation: the guard has to be
    checked where it is installed, not where it is defined.
    """
    import ast
    from pathlib import Path

    conftest = Path(__file__).resolve().parents[1] / "conftest.py"
    tree = ast.parse(conftest.read_text(encoding="utf-8"), filename=str(conftest))
    calls = [
        node
        for node in tree.body
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "guard_configured_url"
    ]
    assert len(calls) == 1, (
        "tests/conftest.py must call guard_configured_url() exactly once at module level; "
        f"found {len(calls)}. Without it the suite can be pointed at production (C-9)."
    )

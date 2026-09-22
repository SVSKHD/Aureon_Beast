"""The schema comparison, and the things that can be decided without a server.

The revision arithmetic is the part most likely to be quietly wrong, and it is the part
that decides whether an operator is told "migrate" or "you are running the wrong build" --
opposite remedies, so getting it backwards is worse than not checking at all.
"""

from __future__ import annotations

import pytest

from aureon.storage.postgres import schema
from aureon.storage.postgres.models import Base
from aureon.storage.postgres.schema import SchemaState, SchemaStatus

#: C-5's list, verbatim from the phase prompt, pinned as a literal.
EXPECTED_TABLES = {
    "detections",
    "detection_evaluations",
    "setups",
    "setup_events",
    "setup_evaluations",
    "sessions",
    "trade_requests",
    "trades",
    "control_requests",
    "audit_logs",
    "heartbeats",
    "system_state",
    "settings",
    "symbol_specs",
    "daily_reviews",
    "weekly_reviews",
    "notifications",
    "alerts",
    "assessments",
    "trade_notes",
    "ops_events",
    "market_days",
    "market_day_frames",
    "sync_batches",
}

#: C-7's four: additive-only, no destructive downgrade, ever.
MONEY_TABLES = {"trade_requests", "trades", "audit_logs", "control_requests"}


# ── Every domain has a table (C-5) ────────────────────────────────────────────


def test_every_table_the_plan_names_exists() -> None:
    """The list is the deliverable, so it is pinned rather than counted.

    A table quietly missing would not fail anything until the service that needed it was
    switched over in S-4 -- at which point the failure is a missing relation in production
    rather than a red test here.
    """
    import aureon.storage.postgres.tables  # noqa: F401  -- registers them

    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_there_are_exactly_twenty_four() -> None:
    """Stated as a number too, because a set comparison passes if BOTH sides drift."""
    import aureon.storage.postgres.tables  # noqa: F401

    assert len(Base.metadata.tables) == 24


def test_every_table_has_a_primary_key() -> None:
    """A table without one cannot be upserted at its deterministic id.

    Which would mean the outbox's retry -- the whole reason §12's ids are pure functions of
    the candle -- silently appends a duplicate instead of overwriting.
    """
    import aureon.storage.postgres.tables  # noqa: F401

    missing = [
        name for name, table in Base.metadata.tables.items() if not table.primary_key.columns
    ]
    assert not missing, f"no primary key on: {missing}"


def test_every_timestamp_column_is_timezone_aware() -> None:
    """CLAUDE.md's rule, asserted over the whole schema rather than table by table.

    A column declared without a zone would pass every round-trip test -- psycopg hands back
    what it was given -- and would silently shift every stored time by the server's offset.
    """
    import aureon.storage.postgres.tables  # noqa: F401
    from aureon.storage.postgres.models import UtcTimestamp

    naive: list[str] = []
    for name, table in Base.metadata.tables.items():
        for column in table.columns:
            if isinstance(column.type, UtcTimestamp):
                continue
            if type(column.type).__name__ in {"DateTime", "TIMESTAMP"}:
                if not getattr(column.type, "timezone", False):
                    naive.append(f"{name}.{column.name}")
    assert not naive, f"timestamp columns without a time zone: {naive}"


def test_broker_identifiers_are_wide_enough() -> None:
    """An MT5 ticket is ten digits at some brokers; int4 stops at 2147483647.

    An overflow here is a failed write on the money path, AFTER the order has been sent --
    the single worst moment for a write to fail, because the position exists and Aureon's
    record of it does not.
    """
    from sqlalchemy import BigInteger

    import aureon.storage.postgres.tables  # noqa: F401

    wide = {
        ("trade_requests", "order_ticket"),
        ("trade_requests", "position_id"),
        ("trade_requests", "magic"),
        ("trades", "mt5_position_id"),
        ("trades", "magic"),
    }
    narrow: list[str] = []
    for table_name, column_name in sorted(wide):
        column = Base.metadata.tables[table_name].columns[column_name]
        if not isinstance(column.type, BigInteger):
            narrow.append(f"{table_name}.{column_name} is {column.type}")
    assert not narrow, f"broker identifiers must be BIGINT: {narrow}"


# ── C-6: detections keep their time fields ────────────────────────────────────


def test_a_detection_keeps_every_time_field_the_id_depends_on() -> None:
    """C-6. The bar's open, its close, the zone and the broker day -- not one ``detected_at``.

    ``candle_close_utc`` is the one ``detection_id`` hashes (§12): a detection is keyed on
    the instant it became KNOWABLE, which is the close. Collapsing the two would make the
    id depend on a field that no longer exists under that meaning.
    """
    import aureon.storage.postgres.tables  # noqa: F401

    columns = set(Base.metadata.tables["detections"].columns.keys())
    for required in ("candle_close_utc", "candle_time_utc", "timezone", "market_date"):
        assert required in columns, f"detections lost {required} (C-6)"
    assert "detected_at" not in columns, (
        "the time fields were collapsed back into a single detected_at (C-6)"
    )


def test_market_timestamp_is_not_stored() -> None:
    """The one place this deviates from C-6's list, deliberately (decision 341).

    ``market_timestamp`` is ``candle_close_utc`` rendered in ``timezone``. Storing a
    rendering beside the instant it is derived from is precisely the drift ``MarketTime``
    exists to prevent: the two can disagree, and nothing would say which is right.
    """
    import aureon.storage.postgres.tables  # noqa: F401

    assert "market_timestamp" not in Base.metadata.tables["detections"].columns


# ── C-7: the money tables ─────────────────────────────────────────────────────


def test_the_initial_revision_refuses_to_downgrade() -> None:
    """C-7. Dropping these is not a rollback -- MT5 would still hold the positions.

    Imported by file path rather than as a module, because alembic versions are not a
    package and the test must exercise the real file the migration runner loads.
    """
    import importlib.util

    path = schema.SCRIPT_LOCATION / "versions" / "0001_initial.py"
    assert path.exists(), path
    spec = importlib.util.spec_from_file_location("s2_initial", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert set(module.MONEY_TABLES) == MONEY_TABLES
    with pytest.raises(RuntimeError) as raised:
        module.downgrade()
    message = str(raised.value)
    for table in MONEY_TABLES:
        assert table in message, f"the refusal does not name {table}"


def test_the_migration_does_not_import_application_code() -> None:
    """decision 343. A migration that has already run must not break when a class moves.

    Autogenerate's default renders a ``TypeDecorator`` by its Python path, which both
    referenced a name the file never imported (so it failed at import) and tied the
    migration to ``aureon.storage.postgres.models``.
    """
    path = schema.SCRIPT_LOCATION / "versions" / "0001_initial.py"
    source = path.read_text(encoding="utf-8")
    assert "aureon.storage" not in source, "the migration references application code"
    assert "sa.DateTime(timezone=True)" in source


# ── The revision comparison ───────────────────────────────────────────────────


def test_the_head_on_disk_is_the_revision_the_code_expects() -> None:
    """A migration added without bumping ``EXPECTED_REVISION`` would make every deployment
    report "behind" forever, and the remedy it printed would never help."""
    assert schema.head_revision() == schema.EXPECTED_REVISION


class _FakeDatabase:
    """A database that answers one question: what revision is stamped in it."""

    def __init__(self, revision: str | None) -> None:
        self.revision = revision

    def dispose(self) -> None:
        return None


def _state(found: str | None, *, expected: str = "0001") -> SchemaState:
    """``compare`` with the revision lookup stubbed, so no server is needed."""
    import aureon.storage.postgres.schema as module

    original = module.database_revision
    module.database_revision = lambda database: database.revision  # type: ignore[assignment]
    try:
        return module.compare(_FakeDatabase(found), expected=expected)
    finally:
        module.database_revision = original  # type: ignore[assignment]


def test_a_matching_revision_is_current() -> None:
    state = _state("0001")
    assert state.status is SchemaStatus.CURRENT
    assert state.ok


def test_a_database_that_was_never_migrated_says_so() -> None:
    state = _state(None)
    assert state.status is SchemaStatus.EMPTY
    assert not state.ok
    assert "migrate.py upgrade" in state.render()


def test_a_newer_database_is_AHEAD_and_the_remedy_is_not_to_migrate() -> None:
    """C-7's dangerous direction, and the one a ``>=`` check misses entirely.

    An older build against a newer schema reads the tables it knows, writes the columns it
    knows, and leaves every column added since NULL -- a broker ticket that never gets
    recorded, in a run that reports success. Telling that operator to migrate would be
    telling them to do nothing and believe it worked.
    """
    state = _state("0099")
    assert state.status is SchemaStatus.AHEAD
    rendered = state.render()
    assert "AHEAD" in rendered
    assert "migrate" not in rendered.lower().replace("migrated", ""), (
        f"the AHEAD remedy must not be 'migrate': {rendered}"
    )


def test_an_unknown_revision_is_treated_as_newer_not_older() -> None:
    """A revision this build has never heard of can only have been written by a newer one.

    Calling it "behind" would tell an operator to migrate, which would then do nothing and
    leave them believing the check passed.
    """
    assert _state("something-else").status is SchemaStatus.AHEAD


def test_the_rendered_line_never_contains_a_password() -> None:
    """The row is printed, logged, and pasted into evidence files."""
    state = SchemaState(SchemaStatus.UNKNOWN, "0001", None, "postgresql://u:***@h/db")
    assert "***" in state.render()


def test_autogenerate_renders_timestamps_as_the_core_type() -> None:
    """``render_item`` itself, not the file it already produced (decision 343).

    A plant breaking the hook survived the test that reads ``0001_initial.py``, and rightly:
    that file is already generated, so the hook's behaviour is only observable by calling
    it. Without this test a broken hook would be found by the NEXT person to autogenerate a
    migration, in the form of a file that fails at import.
    """
    from sqlalchemy import Integer

    from aureon.storage.postgres.migrations import env_module
    from aureon.storage.postgres.models import UtcTimestamp

    assert env_module.render_item("type", UtcTimestamp(), None) == "sa.DateTime(timezone=True)"
    # Everything else falls through to alembic's own rendering.
    assert env_module.render_item("type", Integer(), None) is False
    assert env_module.render_item("column", UtcTimestamp(), None) is False

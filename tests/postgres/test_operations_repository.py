"""Heartbeats, per-symbol state, settings and symbol specs on a real server (S-3b).

The interesting one is ``set_trading_enabled``. Everything else here is an upsert at a
known key with a throttle in front of it; the kill switch is a transaction with a row lock,
a version check and an audit row that must commit with it -- so it gets the contention test
and the "what happens when the audit write fails" test, and the others get round trips.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from aureon.models.settings import ExecutionSettings, NotificationSettings
from aureon.storage.postgres.database import Database
from aureon.storage.postgres.repositories.audit import AuditRepository
from aureon.storage.postgres.repositories.operations import (
    EXECUTION_SETTINGS,
    ExecutionSettingsRepository,
    HeartbeatRepository,
    NotificationSettingsRepository,
    SettingsVersionConflict,
    SymbolRepository,
    SystemStateRepository,
    audit_id_for_settings,
    system_state_id,
)
from tests.postgres.contention import interleave
from tests.postgres.factories import (
    CLOSE,
    a_symbol_spec,
    a_symbol_state,
    a_system_state,
)

pytestmark = pytest.mark.postgres


# ── The id ────────────────────────────────────────────────────────────────────


def test_the_state_id_is_symbol_and_timeframe() -> None:
    assert system_state_id("XAUUSD", "M5") == "XAUUSD_M5"


def test_the_state_id_takes_an_enum_or_its_value() -> None:
    from aureon.models.enums import Timeframe

    assert system_state_id("XAUUSD", Timeframe.M5) == system_state_id("XAUUSD", "M5")


@pytest.mark.parametrize(("symbol", "timeframe"), [("", "M5"), ("XAUUSD", "")])
def test_the_state_id_refuses_an_empty_part(symbol: str, timeframe: str) -> None:
    with pytest.raises(ValueError):
        system_state_id(symbol, timeframe)


# ── Heartbeats ────────────────────────────────────────────────────────────────


def test_a_heartbeat_survives_the_database_unchanged(schema: Database) -> None:
    repo = HeartbeatRepository(schema)
    assert repo.beat("observer", instance_id="host-1", detail={"symbols": 2}, now=CLOSE)

    stored = repo.read("observer")
    assert stored is not None
    assert stored.service == "observer"
    assert stored.instance_id == "host-1"
    assert stored.detail == {"symbols": 2}
    assert stored.updated_at == CLOSE


def test_a_second_beat_inside_the_interval_is_not_written(schema: Database) -> None:
    """The throttle. A service beating on every tick would push a row's worth of WAL per
    tick and tell a reader nothing a five-second-old row did not already say."""
    repo = HeartbeatRepository(schema, min_interval_seconds=5.0)
    assert repo.beat("observer", now=CLOSE)
    assert not repo.beat("observer", now=CLOSE + timedelta(seconds=2))

    stored = repo.read("observer")
    assert stored is not None and stored.updated_at == CLOSE


def test_the_throttle_is_bypassed_by_force(schema: Database) -> None:
    """A candle close is genuinely new information and must not wait for a timer."""
    repo = HeartbeatRepository(schema, min_interval_seconds=5.0)
    repo.beat("observer", now=CLOSE)
    assert repo.beat("observer", force=True, now=CLOSE + timedelta(seconds=1))

    stored = repo.read("observer")
    assert stored is not None and stored.updated_at == CLOSE + timedelta(seconds=1)


def test_the_throttle_is_per_service(schema: Database) -> None:
    repo = HeartbeatRepository(schema, min_interval_seconds=5.0)
    repo.beat("observer", now=CLOSE)
    assert repo.beat("executor", now=CLOSE + timedelta(seconds=1))


def test_read_all_returns_every_service_that_has_beaten(schema: Database) -> None:
    repo = HeartbeatRepository(schema)
    repo.beat("observer", now=CLOSE)
    repo.beat("executor", now=CLOSE)

    assert sorted(repo.read_all()) == ["executor", "observer"]


def test_read_all_finds_a_service_no_registry_lists(schema: Database) -> None:
    """The table is the list. A service added without touching ``paths.SERVICES`` appears
    rather than being silently invisible, which is what the per-name read would have done."""
    HeartbeatRepository(schema).beat("something_new", now=CLOSE)

    assert "something_new" in HeartbeatRepository(schema).read_all()


def test_an_unknown_service_reads_as_none(schema: Database) -> None:
    assert HeartbeatRepository(schema).read("nobody") is None


# ── System state ──────────────────────────────────────────────────────────────


def test_one_row_is_written_per_symbol(schema: Database) -> None:
    repo = SystemStateRepository(schema)
    written = repo.write(
        a_system_state(a_symbol_state("XAUUSD"), a_symbol_state("XAGUSD")),
        force=True,
        now=CLOSE,
    )

    assert written
    assert repo.read_symbol("XAUUSD", "M5") is not None
    assert repo.read_symbol("XAGUSD", "M5") is not None


def test_a_symbols_row_holds_only_its_own_symbol(schema: Database) -> None:
    """A reader of XAGUSD_M5 must not find XAUUSD's panel inside it and have to work out
    which one is current."""
    repo = SystemStateRepository(schema)
    repo.write(
        a_system_state(a_symbol_state("XAUUSD"), a_symbol_state("XAGUSD")),
        force=True,
        now=CLOSE,
    )

    one = repo.read_symbol("XAGUSD", "M5")
    assert one is not None
    assert [s.symbol for s in one.symbols] == ["XAGUSD"]


def test_the_global_fields_travel_with_every_symbols_row(schema: Database) -> None:
    """Derived copies, so a reader of one symbol needs one read."""
    repo = SystemStateRepository(schema)
    repo.write(a_system_state(trading_enabled=True), force=True, now=CLOSE)

    one = repo.read_symbol("XAUUSD", "M5")
    assert one is not None and one.trading_enabled is True


def test_a_state_with_no_symbols_writes_nothing(schema: Database) -> None:
    repo = SystemStateRepository(schema)
    assert not repo.write(a_system_state(symbols=()), force=True, now=CLOSE)


def test_the_state_throttle_is_per_symbol(schema: Database) -> None:
    """The reason there is a row per symbol at all: with one shared row, gold's close
    resets the timer and silver is suppressed for the next few seconds."""
    repo = SystemStateRepository(schema, min_interval_seconds=5.0)
    repo.write(a_system_state(a_symbol_state("XAUUSD")), now=CLOSE)

    assert repo.write(
        a_system_state(a_symbol_state("XAGUSD")), now=CLOSE + timedelta(seconds=1)
    )


def test_a_throttled_write_leaves_the_stored_row_alone(schema: Database) -> None:
    repo = SystemStateRepository(schema, min_interval_seconds=5.0)
    repo.write(a_system_state(a_symbol_state("XAUUSD", detections_today=1)), now=CLOSE)
    repo.write(
        a_system_state(a_symbol_state("XAUUSD", detections_today=9)),
        now=CLOSE + timedelta(seconds=1),
    )

    one = repo.read_symbol("XAUUSD", "M5")
    assert one is not None and one.symbols[0].detections_today == 1


def test_read_merges_every_symbol_into_one_state(schema: Database) -> None:
    repo = SystemStateRepository(schema)
    repo.write(a_system_state(a_symbol_state("XAUUSD")), force=True, now=CLOSE)
    repo.write(
        a_system_state(a_symbol_state("XAGUSD")),
        force=True,
        now=CLOSE + timedelta(seconds=30),
    )

    merged = repo.read()
    assert merged is not None
    assert [s.symbol for s in merged.symbols] == ["XAGUSD", "XAUUSD"]


def test_merged_freshness_is_the_newest_row(schema: Database) -> None:
    """A merged freshness that took the OLDEST would make a quiet symbol look like a dead
    observer, which is the opposite of what a staleness check is for."""
    repo = SystemStateRepository(schema)
    repo.write(a_system_state(a_symbol_state("XAUUSD")), force=True, now=CLOSE)
    later = CLOSE + timedelta(minutes=5)
    repo.write(a_system_state(a_symbol_state("XAGUSD")), force=True, now=later)

    merged = repo.read()
    assert merged is not None and merged.updated_at == later


def test_read_is_none_when_nothing_has_been_written(schema: Database) -> None:
    assert SystemStateRepository(schema).read() is None


def test_updated_at_is_readable_without_parsing_the_snapshot(schema: Database) -> None:
    repo = SystemStateRepository(schema)
    repo.write(a_system_state(), force=True, now=CLOSE)

    assert repo.updated_at("XAUUSD", "M5") == CLOSE


def test_updated_at_answers_for_a_process_that_did_not_write(schema: Database) -> None:
    """``last_write`` only knows this process's own writes; a restarted service needs the
    column, and this is the difference between them."""
    SystemStateRepository(schema).write(a_system_state(), force=True, now=CLOSE)

    fresh = SystemStateRepository(schema)
    assert fresh.last_write is None
    assert fresh.updated_at("XAUUSD", "M5") == CLOSE


# ── Symbol specs ──────────────────────────────────────────────────────────────


def test_a_spec_survives_the_database_unchanged(schema: Database) -> None:
    repo = SymbolRepository(schema)
    repo.publish(a_symbol_spec(), now=CLOSE)

    assert repo.get("XAUUSD") == a_symbol_spec()


def test_publishing_twice_overwrites_rather_than_duplicates(schema: Database) -> None:
    repo = SymbolRepository(schema)
    repo.publish(a_symbol_spec(digits=2), now=CLOSE)
    repo.publish(a_symbol_spec(digits=3), now=CLOSE + timedelta(minutes=1))

    stored = repo.get("XAUUSD")
    assert stored is not None and stored.digits == 3


def test_published_at_is_metadata_and_not_part_of_the_spec(schema: Database) -> None:
    """The spec read back is what the broker said; when the copy was taken is a column."""
    repo = SymbolRepository(schema)
    repo.publish(a_symbol_spec(), now=CLOSE)

    assert repo.published_at("XAUUSD") == CLOSE
    assert repo.get("XAUUSD") == a_symbol_spec()


def test_an_unpublished_symbol_reads_as_none(schema: Database) -> None:
    repo = SymbolRepository(schema)
    assert repo.get("XAGUSD") is None
    assert repo.published_at("XAGUSD") is None


# ── Settings ──────────────────────────────────────────────────────────────────


def test_no_settings_row_means_trading_is_off(schema: Database) -> None:
    """§22's fail-closed default. The opposite would turn an absent row into an open bot."""
    settings = ExecutionSettingsRepository(schema).read_or_default()

    assert settings.trading_enabled is False


def test_read_is_none_rather_than_a_default_when_nothing_is_stored(
    schema: Database,
) -> None:
    """The two are not the same question: ``read`` answers "is there a row", and a caller
    writing settings needs to know that before it overwrites someone's."""
    assert ExecutionSettingsRepository(schema).read() is None


def test_execution_settings_survive_the_database_unchanged(schema: Database) -> None:
    repo = ExecutionSettingsRepository(schema)
    written = repo.write(ExecutionSettings(max_lot=0.5, allowed_symbols=("XAUUSD",)))

    assert repo.read() == written


def test_notification_settings_have_their_own_row(schema: Database) -> None:
    ExecutionSettingsRepository(schema).write(ExecutionSettings())
    notifications = NotificationSettingsRepository(schema)
    notifications.write(NotificationSettings(detections_enabled=False))

    assert notifications.read_or_default().detections_enabled is False
    assert ExecutionSettingsRepository(schema).read_or_default().trading_enabled is False


def test_notification_defaults_are_the_shipped_ones(schema: Database) -> None:
    assert NotificationSettingsRepository(schema).read_or_default() == NotificationSettings()


# ── The kill switch ───────────────────────────────────────────────────────────


def test_enabling_trading_writes_the_row_and_bumps_the_version(schema: Database) -> None:
    repo = ExecutionSettingsRepository(schema)
    updated = repo.set_trading_enabled(True, actor="trader", now=CLOSE)

    assert updated.trading_enabled is True
    assert updated.settings_version == 1
    assert repo.read_or_default().trading_enabled is True


def test_disabling_records_the_reason_and_enabling_clears_it(schema: Database) -> None:
    repo = ExecutionSettingsRepository(schema)
    repo.set_trading_enabled(True, actor="trader", now=CLOSE)
    off = repo.set_trading_enabled(False, actor="trader", reason="spread", now=CLOSE)
    assert off.disabled_reason == "spread"

    on = repo.set_trading_enabled(True, actor="trader", now=CLOSE)
    assert on.disabled_reason is None


def test_the_switch_and_its_audit_row_commit_together(schema: Database) -> None:
    """§60, §71. Separately, the switch could flip with no audit row -- a gap in the trail
    on the one setting that decides whether real money can move."""
    repo = ExecutionSettingsRepository(schema)
    repo.set_trading_enabled(True, actor="trader", now=CLOSE)
    updated = repo.set_trading_enabled(False, actor="trader", reason="news", now=CLOSE)

    record = AuditRepository(schema).get(audit_id_for_settings(updated.settings_version))
    assert record is not None
    assert record.action == "trading.disable"
    assert record.document_id == EXECUTION_SETTINGS
    # The trail records what it moved FROM as well as to; a row that only said "off" could
    # not tell a genuine stop from a no-op re-issue of one.
    assert record.from_status == "True"
    assert record.to_status == "False"
    assert record.reason == "news"


def test_the_audit_row_names_the_table_the_change_is_in(schema: Database) -> None:
    """Decision 356: derived from ``__tablename__``, so the label cannot drift from the
    rows it describes."""
    from aureon.storage.postgres import tables

    updated = ExecutionSettingsRepository(schema).set_trading_enabled(
        True, actor="trader", now=CLOSE
    )
    record = AuditRepository(schema).get(audit_id_for_settings(updated.settings_version))

    assert record is not None
    assert record.collection == tables.Setting.__tablename__


def test_a_failed_audit_write_takes_the_switch_with_it(schema: Database) -> None:
    """The half-written case, forced. An audit row that lands for a write that failed is
    worse than none: it claims something happened that did not."""
    repo = ExecutionSettingsRepository(schema)

    class Exploding(AuditRepository):
        def append(self, record: Any, *, connection: Any = None) -> str:
            raise RuntimeError("audit is down")

    repo.audit = Exploding(schema)
    with pytest.raises(RuntimeError):
        repo.set_trading_enabled(True, actor="trader", now=CLOSE)

    assert ExecutionSettingsRepository(schema).read() is None


def test_a_stale_version_is_refused_rather_than_overwritten(schema: Database) -> None:
    """For a UI that read the settings, rendered a button, and got a press seconds later."""
    repo = ExecutionSettingsRepository(schema)
    repo.set_trading_enabled(True, actor="trader", now=CLOSE)

    with pytest.raises(SettingsVersionConflict):
        repo.set_trading_enabled(False, actor="other", if_version=0, now=CLOSE)

    assert repo.read_or_default().trading_enabled is True


def test_the_current_version_is_accepted(schema: Database) -> None:
    repo = ExecutionSettingsRepository(schema)
    first = repo.set_trading_enabled(True, actor="trader", now=CLOSE)

    second = repo.set_trading_enabled(
        False, actor="trader", if_version=first.settings_version, now=CLOSE
    )
    assert second.settings_version == first.settings_version + 1


class _Signalling(ExecutionSettingsRepository):
    """Announces when it holds the settings row, still inside its transaction."""

    def __init__(self, database: Database, signal) -> None:  # type: ignore[no-untyped-def]
        super().__init__(database)
        self._signal = signal

    def _locked(self, connection):  # type: ignore[no-untyped-def]
        row = super()._locked(connection)
        self._signal()
        return row


def test_two_toggles_racing_resolve_to_one_winner(schema: Database) -> None:
    """Decision 352's discipline applied to the kill switch.

    Without the lock both read version 0, both write version 1 and one of them vanishes --
    and the direction that vanishes might be the safe one. With it, the second waits, sees
    version 1 and writes version 2, so the trail has both decisions in order.
    """
    outcome = interleave(
        lambda signal: _Signalling(schema, signal).set_trading_enabled(
            True, actor="a", now=CLOSE
        ),
        lambda: ExecutionSettingsRepository(schema).set_trading_enabled(
            False, actor="b", reason="stop", now=CLOSE
        ),
    )

    assert outcome.failed == [], outcome
    versions = sorted(one.settings_version for one in outcome.results.values())
    assert versions == [1, 2], outcome

    audit = AuditRepository(schema)
    assert audit.get(audit_id_for_settings(1)) is not None
    assert audit.get(audit_id_for_settings(2)) is not None


def test_a_racing_version_assertion_loses_rather_than_overwrites(schema: Database) -> None:
    """The second toggle asserts the version it read before the first ran. Under the lock
    it is refused; without one it would silently undo the first."""
    outcome = interleave(
        lambda signal: _Signalling(schema, signal).set_trading_enabled(
            True, actor="a", now=CLOSE
        ),
        lambda: ExecutionSettingsRepository(schema).set_trading_enabled(
            False, actor="b", if_version=0, now=CLOSE
        ),
    )

    assert [type(e).__name__ for e in outcome.errors.values()] == [
        "SettingsVersionConflict"
    ], outcome
    assert ExecutionSettingsRepository(schema).read_or_default().trading_enabled is True

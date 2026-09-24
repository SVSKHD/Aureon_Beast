"""Runtime guardian: planned updates, weekend suppression and one-shot stale-feed restart."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from aureon.config import AureonConfig
from aureon.services.runtime_guardian import RuntimeGuardian

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)  # Saturday


class FakeSystemState:
    def __init__(self, tick_at):
        self.tick_at = tick_at

    def read_symbol(self, symbol, timeframe):
        return SimpleNamespace(
            symbols=(SimpleNamespace(last_tick_at=self.tick_at, last_quote=None),)
        )


class FakeStorage:
    def __init__(self, tick_at):
        self.system_state = FakeSystemState(tick_at)


class RecordingOps:
    def __init__(self):
        self.calls = []

    def observe(self, name, **kwargs):
        self.calls.append((name, kwargs))


def guardian(
    tmp_path: Path,
    *,
    now: datetime,
    tick_at: datetime,
    auto_restart: bool = True,
    ops=None,
) -> RuntimeGuardian:
    return RuntimeGuardian(
        repo_root=tmp_path,
        config=AureonConfig(symbols=("XAUUSD",)),
        storage=FakeStorage(tick_at),
        ops=ops,
        poll_seconds=5,
        stale_restart_seconds=3 * 60 * 60,
        auto_update_main=False,
        auto_restart_stale_feed=auto_restart,
        now=lambda: now,
    )


def test_weekend_three_hour_silence_is_market_holiday_not_restart(tmp_path) -> None:
    ops = RecordingOps()
    g = guardian(
        tmp_path,
        now=NOW,
        tick_at=NOW - timedelta(hours=4),
        ops=ops,
    )

    assert g._check_market_liveness() is None
    holiday = [call for call in ops.calls if call[0] == "market_holiday"]
    assert holiday
    assert holiday[-1][1]["active"] is True
    assert holiday[-1][1]["scope"] == "XAUUSD"


def test_open_market_three_hour_silence_requests_one_restart(tmp_path) -> None:
    monday = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
    tick = monday - timedelta(hours=4)
    g = guardian(tmp_path, now=monday, tick_at=tick)

    first = g._check_market_liveness()
    assert first is not None
    assert first.reason == "stale market feed"
    assert "4.0h" in first.detail

    second = g._check_market_liveness()
    assert second is None, "same stale tick must not create a restart loop"


def test_a_new_stale_tick_may_request_a_later_restart(tmp_path) -> None:
    monday = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
    g = guardian(tmp_path, now=monday, tick_at=monday - timedelta(hours=4))
    assert g._check_market_liveness() is not None

    later = monday + timedelta(hours=5)
    g._now = lambda: later
    g.storage.system_state.tick_at = monday + timedelta(minutes=30)
    again = g._check_market_liveness()

    assert again is not None
    assert again.reason == "stale market feed"


def test_stale_feed_restart_can_be_disabled_without_hiding_the_condition(tmp_path) -> None:
    monday = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
    g = guardian(
        tmp_path,
        now=monday,
        tick_at=monday - timedelta(hours=4),
        auto_restart=False,
    )

    assert g._check_market_liveness() is None

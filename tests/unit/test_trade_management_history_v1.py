"""Durable Agent14/16 management-history tests for V1 exit learning."""

from datetime import UTC, datetime

from aureon.management.profit_guardian import ProfitGuardianAgent
from aureon.management.trade_manager import TradeManagementAgent
from aureon.models.base import MarketTime
from aureon.models.enums import Direction
from aureon.models.trade import Trade, TradeManagementEvent
from aureon.storage.local_database import LocalDatabase
from aureon.storage.postgres.repositories.trades import TradeRepository


def test_management_state_and_event_history_are_persistent(tmp_path) -> None:
    db = LocalDatabase(tmp_path / "aureon.db")
    db.ensure_schema()
    repo = TradeRepository(db)

    trade = Trade(
        trade_id="primary__42",
        mt5_position_id=42,
        symbol="XAUUSD",
        direction=Direction.BUY,
        volume=0.25,
        open_price=4500.0,
        open_time=MarketTime.from_utc(datetime(2026, 3, 1, tzinfo=UTC), "UTC"),
    )
    repo.upsert_open(trade)

    management = TradeManagementAgent().assess(
        direction=Direction.BUY,
        entry_price=4500.0,
        current_price=4505.5,
        peak_price=4505.5,
    )
    guardian = ProfitGuardianAgent().assess(
        direction=Direction.BUY,
        entry_price=4500.0,
        current_price=4505.5,
        peak_price=4505.5,
        market_health={"ema": True, "htf": True},
    )
    updated = repo.update_management(
        trade.trade_id,
        management=management,
        guardian=guardian,
    )
    assert updated is not None
    assert updated.management is not None
    assert updated.guardian is not None
    assert updated.guardian.active is True

    observed = datetime(2026, 3, 1, 1, 0, tzinfo=UTC)
    event = TradeManagementEvent(
        event_id="mgmt-1",
        trade_id=trade.trade_id,
        observed_at=observed,
        source="profit_guardian",
        action="trail",
        current_move=5.5,
        peak_move=5.5,
        giveback=0.0,
        protected_move=4.0,
        trail_price=4504.0,
    )
    repo.append_management_event(event)
    # Idempotent replay of the same observation must not duplicate it.
    repo.append_management_event(event)

    history = repo.management_events(trade.trade_id)
    assert len(history) == 1
    assert history[0].protected_move == 4.0
    assert history[0].trail_price == 4504.0

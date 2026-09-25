"""EOD training memory uses frozen setup evidence and completed-day labels."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aureon.models.base import MarketTime
from aureon.models.enums import (
    DirectionContext,
    SetupAnchorKind,
    SetupEventType,
    SetupFamily,
    SetupState,
    Timeframe,
)
from aureon.models.market_day import FrameBar, MarketDay, MarketDayFrame
from aureon.models.setup import Setup, SetupAnchor, SetupEvent
from aureon.services.eod_training_agent import EodTrainingAgent

NOW = datetime(2026, 9, 25, 16, 0, tzinfo=UTC)
TZ = "Europe/Athens"
DATE = "2026-09-25"


class _Events:
    def __init__(self, rows):
        self.rows = list(rows)

    def for_setup(self, setup_id: str):
        return [row for row in self.rows if row.setup_id == setup_id]


class _Setups:
    def __init__(self, setup, events):
        self.setup = setup
        self.events = _Events(events)

    def for_market_date(self, symbol: str, market_date: str):
        if symbol == self.setup.symbol and market_date == self.setup.market_date:
            return [self.setup]
        return []


class _Evaluations:
    def get_many(self, setups, rule_id: str):
        return {}


class _MarketDays:
    def __init__(self, *, complete: bool, frame: MarketDayFrame | None):
        self.day = MarketDay(
            symbol="XAUUSD",
            market_date=DATE,
            market_tz=TZ,
            complete=complete,
        )
        self.frame = frame

    def get_day(self, symbol: str, market_date: str):
        return self.day if symbol == "XAUUSD" and market_date == DATE else None

    def get_frame(self, symbol: str, market_date: str, timeframe: Timeframe):
        return self.frame


class _Memory:
    def __init__(self):
        self.examples = []
        self.statuses = []

    def write_example(self, row):
        self.examples.append(row)
        return row

    def write_status(self, row):
        self.statuses.append(row)
        return row


def _setup() -> Setup:
    return Setup(
        setup_id="setup-1",
        account_scope="primary",
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        family=SetupFamily.LIQUIDITY_REVERSAL,
        direction_context=DirectionContext.BULLISH,
        state=SetupState.CONFIRMED,
        market_date=DATE,
        anchor=SetupAnchor(
            kind=SetupAnchorKind.LIQUIDITY_LEVEL,
            price=2400.0,
            level_type="previous_day_low",
        ),
        opened_at=NOW - timedelta(hours=2),
        confirmed_at=NOW - timedelta(hours=1),
        linked_detection_ids=("d1",),
    )


def _event(
    event_type: SetupEventType,
    *,
    at: datetime,
    snapshot: dict[str, str],
) -> SetupEvent:
    return SetupEvent(
        event_id=f"{event_type.value}-{int(at.timestamp())}",
        setup_id="setup-1",
        event_type=event_type,
        from_state=SetupState.CONFIRMED,
        to_state=SetupState.CONFIRMED,
        market_time=MarketTime.from_utc(at, TZ),
        context_snapshot=snapshot,
        reason=event_type.value,
    )


def _agent(*, complete: bool = True, reached: bool = True):
    setup = _setup()
    start = NOW - timedelta(minutes=30)
    confirm = _event(
        SetupEventType.CONFIRMED,
        at=NOW - timedelta(hours=1),
        snapshot={
            "ema_fast": "2401.0",
            "ema_slow": "2400.0",
            "rsi": "58.0",
            "trend": "bullish",
            "agent_confidence_pct": "67",
            "agent_ema_cross_stance": "bullish",
            "agent_ema_cross_alignment": "aligned",
            "agent_ema_cross_observation": "fast above slow",
            "agent_rsi_stance": "bullish",
            "agent_rsi_alignment": "aligned",
            "agent_rsi_observation": "58 above midpoint",
        },
    )
    tracking = _event(
        SetupEventType.FAVOURABLE_MOVE_6_TRACKING,
        at=start,
        snapshot={
            "reference_detection_id": "d1",
            "reference_price": "2400.00000",
            "threshold_price": "2406.00000",
        },
    )
    events = [confirm, tracking]
    reached_at = start + timedelta(minutes=15)
    if reached:
        events.append(
            _event(
                SetupEventType.FAVOURABLE_MOVE_6_REACHED,
                at=reached_at,
                snapshot={
                    "reference_detection_id": "d1",
                    "reference_price": "2400.00000",
                    "observed_extreme": "2406.20000",
                    "favourable_excursion": "6.20000",
                },
            )
        )

    bars = (
        FrameBar(
            at=start,
            open=2400.0,
            high=2402.0,
            low=2398.5,
            close=2401.0,
        ),
        FrameBar(
            at=start + timedelta(minutes=5),
            open=2401.0,
            high=2404.0,
            low=2399.0,
            close=2403.0,
        ),
        FrameBar(
            at=start + timedelta(minutes=10),
            open=2403.0,
            high=2406.2,
            low=2402.0,
            close=2405.5,
        ),
    )
    frame = MarketDayFrame(
        symbol="XAUUSD",
        market_date=DATE,
        timeframe=Timeframe.M5,
        market_tz=TZ,
        bars=bars,
        complete=True,
    )
    memory = _Memory()
    agent = EodTrainingAgent(
        setups=_Setups(setup, events),
        setup_evaluations=_Evaluations(),
        market_days=_MarketDays(complete=complete, frame=frame),
        memory=memory,
        rule_id="XAU_OUTCOME_V2",
        now=lambda: NOW,
    )
    return agent, memory


def test_eod_training_freezes_agent_decisions_and_six_dollar_label() -> None:
    agent, memory = _agent()

    status = agent.build_day(symbol="XAUUSD", market_date=DATE)

    assert status.examples_written == 1
    assert status.reached_six == 1
    assert status.not_reached_six == 0
    row = memory.examples[0]
    assert row.context["ema_fast"] == 2401.0
    assert row.context["rsi"] == 58.0
    assert row.agent_read["ema_cross"]["alignment"] == "aligned"
    assert row.six_dollar_reached is True
    assert row.time_to_six_seconds == 900.0
    assert row.mae_before_six_price == pytest.approx(1.5)
    assert status.median_mae_before_six_price == pytest.approx(1.5)
    assert status.max_mae_before_six_price == pytest.approx(1.5)
    assert status.by_timeframe[0].median_mae_before_six_price == pytest.approx(1.5)


def test_completed_day_without_six_dollar_reach_is_a_negative_eod_label() -> None:
    agent, memory = _agent(reached=False)

    status = agent.build_day(symbol="XAUUSD", market_date=DATE)

    assert status.reached_six == 0
    assert status.not_reached_six == 1
    assert memory.examples[0].six_dollar_status == "not_reached_eod"
    assert memory.examples[0].six_dollar_reached is False


def test_incomplete_day_is_never_frozen_as_a_training_miss() -> None:
    agent, _ = _agent(complete=False, reached=False)

    with pytest.raises(ValueError, match="not stored as complete"):
        agent.build_day(symbol="XAUUSD", market_date=DATE)


def test_training_checkpoint_identity_includes_contract_versions() -> None:
    agent, _ = _agent()
    status = agent.build_day(symbol="XAUUSD", market_date=DATE)

    assert "EOD_SETUP_FEATURES_V1" in status.status_id
    assert "FAVOURABLE_MOVE_6_V1" in status.status_id

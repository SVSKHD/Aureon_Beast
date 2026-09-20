"""The live market snapshot and the §59 ``/status`` layout.

Two properties earn tests here.

**The snapshot never recomputes an indicator.** Every value comes from a detection's own
``IndicatorSnapshot`` — the values that were stored. A second implementation would, the
first time the two disagreed, make the status screen quietly wrong about a number a human
was using to decide something.

**A missing value renders as a dash, not a zero.** A zero is a measurement; a dash is the
absence of one. On a screen that reports "how far apart are the EMAs", rendering an
unknown as 0.00 says the EMAs have converged.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aureon.discord.service import (
    UNKNOWN,
    build_live_panel,
    build_status,
)
from aureon.models.base import MarketTime
from aureon.models.detection import Detection, IndicatorSnapshot, SessionContext
from aureon.models.enums import (
    Direction,
    Freshness,
    MarketState,
    SessionName,
    Timeframe,
)
from aureon.models.identity import detection_id
from aureon.models.market import QuoteSnapshot
from aureon.models.settings import ExecutionSettings
from aureon.models.system import Heartbeat, SymbolState, SystemState
from aureon.services.market_snapshot import MarketSnapshot

TZ = "Europe/Athens"
BASE = datetime(2026, 9, 16, 10, 0, tzinfo=UTC)


def detection(
    *,
    agent: str = "ema_cross",
    event_key: str = "bullish",
    minutes: float = 0.0,
    direction: Direction | None = Direction.BUY,
    ema: dict[str, float] | None = None,
    rsi: float | None = None,
    price: float = 2400.0,
    session: SessionName = SessionName.LONDON,
) -> Detection:
    close = BASE + timedelta(minutes=minutes)
    return Detection(
        detection_id=detection_id(
            account_scope="primary",
            symbol="XAUUSD",
            timeframe="M5",
            candle_close=close,
            agent_name=agent,
            agent_version="2.0.0",
            event_key=event_key,
        ),
        account_scope="primary",
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        agent_name=agent,
        agent_version="2.0.0",
        event_key=event_key,
        direction=direction,
        detected_at=MarketTime.from_utc(close, TZ),
        candle_open_time=MarketTime.from_utc(close - timedelta(minutes=5), TZ),
        price=price,
        indicators=IndicatorSnapshot(ema=ema or {}, rsi=rsi),
        session=SessionContext(session=session, session_config_version=1),
        sequence_today=1,
        sequence_session=1,
    )


# ── The snapshot reports stored values ────────────────────────────────────────


def test_indicators_come_from_the_detections_own_snapshot() -> None:
    """Not recomputed. The screen must report what was stored."""
    snapshot = MarketSnapshot(symbol="XAUUSD")
    snapshot.observe(detection(ema={"fast": 2401.5, "slow": 2399.25}, rsi=71.4))

    assert snapshot.ema_fast == 2401.5
    assert snapshot.ema_slow == 2399.25
    assert snapshot.rsi == 71.4
    assert snapshot.ema_distance == pytest.approx(2.25)
    assert snapshot.rsi_zone == "overbought"


def test_an_unknown_indicator_stays_unknown() -> None:
    """A fresh start says "unknown", which is the true answer before anything looked."""
    snapshot = MarketSnapshot(symbol="XAUUSD")
    assert snapshot.ema_fast is None
    assert snapshot.ema_distance is None
    assert snapshot.rsi_zone is None


def test_a_half_known_ema_pair_yields_no_distance() -> None:
    """A distance from one EMA and a missing one would read as a real number."""
    snapshot = MarketSnapshot(symbol="XAUUSD")
    snapshot.observe(detection(ema={"fast": 2401.5}))
    assert snapshot.ema_fast == 2401.5
    assert snapshot.ema_slow is None
    assert snapshot.ema_distance is None


def test_the_rsi_zone_uses_the_same_thresholds_as_the_agent() -> None:
    """One implementation, so /status and the agent cannot disagree."""
    from aureon.agents.rsi_agent import rsi_zone

    snapshot = MarketSnapshot(symbol="XAUUSD")
    for value in (10.0, 29.9, 30.0, 50.0, 70.0, 99.0):
        snapshot.rsi = value
        assert snapshot.rsi_zone == rsi_zone(value, overbought=70.0, oversold=30.0)


# ── Last-of-each-kind ─────────────────────────────────────────────────────────


def test_each_event_kind_lands_in_its_own_slot() -> None:
    snapshot = MarketSnapshot(symbol="XAUUSD")
    snapshot.observe(detection(agent="ema_cross", event_key="bullish", minutes=0))
    snapshot.observe(
        detection(agent="liquidity", event_key="up|swing_high", minutes=5)
    )
    snapshot.observe(
        detection(agent="breakout", event_key="down|swing_low", minutes=10)
    )
    snapshot.observe(
        detection(agent="wick", event_key="lower_rejection", minutes=15, direction=None)
    )
    snapshot.observe(
        detection(agent="session_trend", event_key="london|up", minutes=20, direction=None)
    )

    assert snapshot.last_cross is not None
    assert snapshot.last_cross["direction"] == "buy"
    assert snapshot.last_cross["detection_id"]
    assert snapshot.last_sweep == {
        "direction": "up",
        "level_type": "swing_high",
        "at": (BASE + timedelta(minutes=5)).isoformat(),
    }
    assert snapshot.last_breakout is not None
    assert snapshot.last_breakout["level_type"] == "swing_low"
    assert snapshot.last_wick is not None
    assert snapshot.last_wick["classification"] == "lower_rejection"
    assert snapshot.session_trend == "up"


def test_a_later_event_replaces_the_earlier_one() -> None:
    snapshot = MarketSnapshot(symbol="XAUUSD")
    snapshot.observe(detection(minutes=0))
    first = snapshot.last_cross_at
    snapshot.observe(detection(minutes=5, event_key="bearish", direction=Direction.SELL))

    assert snapshot.last_cross["direction"] == "sell"
    assert snapshot.last_cross_at > first


def test_last_cross_at_is_denormalised_from_last_cross() -> None:
    """So freshness needs no dict parsing, and the two cannot disagree."""
    snapshot = MarketSnapshot(symbol="XAUUSD")
    snapshot.observe(detection(minutes=0))
    assert snapshot.last_cross_at.isoformat() == snapshot.last_cross["at"]


# ── Counting and rolling ──────────────────────────────────────────────────────


def test_the_same_detection_is_not_counted_twice() -> None:
    """The restart backfill replays candles it already processed.

    Counting twice would make detections_today drift upward with nothing to reveal it.
    """
    snapshot = MarketSnapshot(symbol="XAUUSD")
    same = detection(minutes=0)
    snapshot.observe(same)
    snapshot.observe(same)
    assert snapshot.detections_today == 1


def test_a_new_broker_day_resets_the_count_but_keeps_the_emas() -> None:
    """An EMA does not reset at midnight.

    Blanking it would make the screen claim no information on the first candle of every
    day -- which is exactly when a reader is most likely to look.
    """
    snapshot = MarketSnapshot(symbol="XAUUSD")
    snapshot.observe(detection(ema={"fast": 2401.0, "slow": 2399.0}, minutes=0))
    assert snapshot.detections_today == 1

    # 2026-09-16 21:00 UTC is 2026-09-17 in Athens.
    snapshot.observe(detection(minutes=11 * 60, session=SessionName.OFF))

    assert snapshot.detections_today == 1, "the day did not roll"
    assert snapshot.ema_fast == 2401.0, "the EMA was blanked by the day roll"


def test_session_extremes_come_from_candles_not_detections() -> None:
    """A session has a high whether or not anything detected anything."""
    snapshot = MarketSnapshot(symbol="XAUUSD")
    snapshot.observe_candle(high=2405.0, low=2395.0, session=SessionName.LONDON)
    snapshot.observe_candle(high=2410.0, low=2398.0, session=SessionName.LONDON)

    assert snapshot.session_high == 2410.0
    assert snapshot.session_low == 2395.0


def test_a_session_change_resets_the_extremes() -> None:
    snapshot = MarketSnapshot(symbol="XAUUSD")
    snapshot.observe_candle(high=2405.0, low=2395.0, session=SessionName.LONDON)
    snapshot.observe_candle(high=2402.0, low=2400.0, session=SessionName.NEW_YORK)

    assert snapshot.session is SessionName.NEW_YORK
    assert snapshot.session_high == 2402.0
    assert snapshot.session_low == 2400.0


# ── The /status layout (§59) ───────────────────────────────────────────────────


def full_symbol_state(**overrides) -> SymbolState:
    base = dict(
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        market_state=MarketState.OPEN,
        last_quote=QuoteSnapshot(symbol="XAUUSD", bid=2400.0, ask=2400.3, point=0.01),
        ema_fast=2401.5,
        ema_slow=2399.25,
        rsi=71.4,
        session=SessionName.LONDON,
        session_trend="up",
        session_high=2405.0,
        session_low=2395.0,
        ema_crosses_today=3,
        ema_crosses_session=2,
        bullish_crosses_today=2,
        bearish_crosses_today=1,
        bullish_crosses_session=1,
        bearish_crosses_session=1,
        last_cross={
            "direction": "buy",
            "at": "2026-09-16T10:05:00+00:00",
            "price": 2400.0,
            "detection_id": "d1",
        },
        last_sweep={
            "direction": "up",
            "level_type": "swing_high",
            "at": "2026-09-16T10:00:00+00:00",
        },
        last_breakout={
            "direction": "down",
            "level_type": "swing_low",
            "at": "2026-09-16T09:55:00+00:00",
        },
        last_wick={
            "classification": "lower_rejection",
            "at": "2026-09-16T09:50:00+00:00",
        },
        detections_today=9,
    )
    return SymbolState(**(base | overrides))


def test_a_full_snapshot_renders_every_field() -> None:
    """The gate: nothing silently missing from the live screen."""
    panel = build_live_panel(full_symbol_state())
    rendered = "\n".join(panel.lines)

    for expected in (
        "2401.50",  # ema fast
        "2399.25",  # ema slow
        "+2.25",  # ema distance, signed
        "71.4",  # rsi
        "overbought",  # rsi zone
        "london",
        "up",  # session trend
        "2405.00",  # session high
        "2395.00",  # session low
        "crosses today 3 (2↑ 1↓)",
        "session 2 (1↑ 1↓)",
        "last cross buy at 10:05:00Z",
        "last sweep up swing_high at 10:00:00Z",
        "last breakout down swing_low at 09:55:00Z",
        "last wick lower_rejection at 09:50:00Z",
        "detections today 9",
    ):
        assert expected in rendered, f"{expected!r} missing from:\n{rendered}"


def test_an_empty_snapshot_renders_dashes_not_zeros() -> None:
    """0.00 would claim the EMAs had converged; a dash says nobody has looked."""
    panel = build_live_panel(
        SymbolState(symbol="XAUUSD", timeframe=Timeframe.M5, market_state=MarketState.OPEN)
    )
    rendered = "\n".join(panel.lines)

    assert rendered.count(UNKNOWN) >= 8
    assert "0.00" not in rendered, "an unknown value rendered as zero"


def test_the_panel_keeps_its_shape_when_values_are_missing() -> None:
    """A panel that hides empty rows changes shape as data arrives."""
    full = build_live_panel(full_symbol_state())
    empty = build_live_panel(
        SymbolState(symbol="XAUUSD", timeframe=Timeframe.M5, market_state=MarketState.OPEN)
    )
    assert len(full.lines) == len(empty.lines)


def test_live_panels_appear_only_on_an_open_market() -> None:
    """On a closed market those numbers are a snapshot of whenever it shut.

    Rendering them in a live layout invites reading them as current, so the closed screen
    shows the completed review instead.
    """
    open_screen = build_status(
        system_state=SystemState(symbols=(full_symbol_state(),)),
        heartbeats={},
        settings=ExecutionSettings(),
    )
    assert open_screen.live_panels
    assert open_screen.review_summary is None

    closed_screen = build_status(
        system_state=SystemState(
            symbols=(full_symbol_state(market_state=MarketState.CLOSED),)
        ),
        heartbeats={},
        settings=ExecutionSettings(),
    )
    assert closed_screen.live_panels == []
    assert closed_screen.review_summary is not None


def test_a_forty_six_second_old_snapshot_reads_stale() -> None:
    """45s is the §59 threshold, so 46 must be the other side of it."""
    now = datetime(2026, 9, 16, 10, 0, tzinfo=UTC)
    stale = now - timedelta(seconds=46)
    screen = build_status(
        system_state=SystemState(updated_at=stale, symbols=(full_symbol_state(),)),
        heartbeats={"observer": Heartbeat(service="observer", updated_at=stale)},
        settings=ExecutionSettings(),
        now=now,
    )
    assert screen.overall is Freshness.STALE
    observer = next(s for s in screen.services if s.name == "observer")
    assert observer.freshness is Freshness.STALE


def test_a_fresh_snapshot_reads_live() -> None:
    now = datetime(2026, 9, 16, 10, 0, tzinfo=UTC)
    fresh = now - timedelta(seconds=5)
    screen = build_status(
        system_state=SystemState(updated_at=fresh, symbols=(full_symbol_state(),)),
        heartbeats={"observer": Heartbeat(service="observer", updated_at=fresh)},
        settings=ExecutionSettings(),
        now=now,
    )
    assert screen.overall is Freshness.LIVE


def test_the_model_derives_distance_and_zone_so_they_cannot_be_inconsistent() -> None:
    """A stored derivation can go stale; deriving on validation means it cannot."""
    state = SymbolState(
        symbol="XAUUSD", timeframe=Timeframe.M5, ema_fast=2401.5, ema_slow=2399.25, rsi=71.4
    )
    assert state.ema_distance == pytest.approx(2.25)
    assert state.rsi_zone == "overbought"

    bare = SymbolState(symbol="XAUUSD", timeframe=Timeframe.M5)
    assert bare.ema_distance is None
    assert bare.rsi_zone is None


# ── The §59 last line (D-7) ───────────────────────────────────────────────────


def test_the_status_screen_reports_when_it_was_last_updated() -> None:
    """§59 asks for "last updated" beside LIVE/STALE/OFFLINE, not the word alone.

    Both the timestamp and the age, because they answer different questions: the age is
    what tells a reader whether to trust the numbers, and the timestamp is what they
    quote when something looks wrong. A freshness word on its own hides how far past the
    threshold a STALE screen has drifted — 46 seconds and six hours read identically.
    """
    now = datetime(2026, 9, 16, 10, 0, tzinfo=UTC)
    screen = build_status(
        system_state=SystemState(
            updated_at=now - timedelta(seconds=12), symbols=(full_symbol_state(),)
        ),
        heartbeats={},
        settings=ExecutionSettings(),
        now=now,
    )

    assert screen.updated_at == now - timedelta(seconds=12)
    assert screen.age_seconds == pytest.approx(12.0)
    assert screen.updated_line == "last updated 09:59:48Z (12s ago) — LIVE"


def test_a_stale_screen_says_how_stale() -> None:
    now = datetime(2026, 9, 16, 10, 0, tzinfo=UTC)
    screen = build_status(
        system_state=SystemState(updated_at=now - timedelta(seconds=46)),
        heartbeats={},
        settings=ExecutionSettings(),
        now=now,
    )
    assert "46s ago" in screen.updated_line
    assert "STALE" in screen.updated_line


def test_a_missing_system_state_says_never_rather_than_guessing() -> None:
    """"never" is a fact. A rendered zero or a blank would both read as "just now"."""
    screen = build_status(
        system_state=None,
        heartbeats={},
        settings=ExecutionSettings(),
        now=datetime(2026, 9, 16, 10, 0, tzinfo=UTC),
    )
    assert screen.updated_at is None
    assert screen.age_seconds is None
    assert screen.updated_line.startswith("last updated never")


def test_every_section_59_label_reaches_the_screen() -> None:
    """D-7's proof: the whole §59 layout, not most of it.

    Checked against the assembled screen rather than a Discord embed so it runs without
    a gateway — the embed is a thin rendering of exactly these values.
    """
    now = datetime(2026, 9, 16, 10, 0, tzinfo=UTC)
    screen = build_status(
        system_state=SystemState(updated_at=now, symbols=(full_symbol_state(),)),
        heartbeats={},
        settings=ExecutionSettings(trading_enabled=True),
        open_trades=2,
        pending_requests=1,
        now=now,
    )
    panel = "\n".join(screen.live_panels[0].lines)

    # Market and price
    assert "2400.00" in panel and "2400.30" in panel  # bid/ask
    assert screen.live_panels[0].market_state == "open"
    # Session, trend, extremes
    assert "london" in panel and "up" in panel
    assert "2405.00" in panel and "2395.00" in panel
    # Indicators
    assert "2401.50" in panel and "2399.25" in panel  # EMA 20/50
    assert "71.4" in panel and "overbought" in panel  # RSI + zone
    # Crosses
    assert "crosses today 3" in panel and "session 2" in panel
    assert "last cross buy at 10:05:00Z" in panel
    # The other agents
    assert "last sweep up swing_high" in panel  # liquidity
    assert "last breakout down swing_low" in panel
    assert "last wick lower_rejection" in panel
    # Account-level
    assert screen.trading_enabled is True
    assert screen.open_trades == 2
    assert screen.pending_requests == 1
    assert len(screen.services) == 4
    # Provenance
    assert "last updated" in screen.updated_line

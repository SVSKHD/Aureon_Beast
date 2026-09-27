"""Daily Market Bias Agent: chronological, deterministic, restart-safe (GAP: new feature)."""

from __future__ import annotations

from aureon.models.enums import Direction, SessionName
from aureon.models.market_bias import DailyBiasState, ReversalRisk
from aureon.services.daily_market_bias import DailyBiasStateStore, DailyMarketBiasAgent
from tests.unit.v1_fixtures import bias_inputs, drift_day


def test_bullish_day_classifies_bullish_and_prefers_buy() -> None:
    snap = drift_day(DailyMarketBiasAgent(), per_bar=0.6)
    assert snap.daily_bias in {DailyBiasState.BULLISH, DailyBiasState.STRONG_BULLISH}
    assert snap.preferred_direction is Direction.BUY
    assert snap.daily_bias_strength > 0.25
    assert snap.aligned(Direction.BUY) is True
    assert snap.aligned(Direction.SELL) is False


def test_bearish_day_classifies_bearish_and_prefers_sell() -> None:
    snap = drift_day(DailyMarketBiasAgent(), per_bar=-0.6)
    assert snap.daily_bias in {DailyBiasState.BEARISH, DailyBiasState.STRONG_BEARISH}
    assert snap.preferred_direction is Direction.SELL
    assert snap.session_aligned(Direction.SELL) is True


def test_flat_day_is_neutral_with_no_preferred_direction() -> None:
    snap = drift_day(DailyMarketBiasAgent(), per_bar=0.0)
    assert snap.daily_bias is DailyBiasState.NEUTRAL
    assert snap.preferred_direction is None
    assert snap.aligned(Direction.BUY) is None


def test_session_turning_against_a_directional_day_is_a_reversal() -> None:
    agent = DailyMarketBiasAgent()
    price = 2000.0
    snap = None
    for index in range(150):
        session = SessionName.LONDON if index < 90 else SessionName.NEW_YORK
        per_bar = 0.7 if index < 90 else -0.9
        snap = agent.observe(
            bias_inputs(
                index,
                close=price + per_bar,
                open_=price,
                session=session,
                ema_fast=price + per_bar,
                ema_slow=price - 3 * per_bar,
                rsi=65 if per_bar > 0 else 35,
                htf="bullish" if index < 100 else "bearish",
                session_trend="up" if per_bar > 0 else "down",
            )
        )
        price += per_bar
        if index == 89:
            assert snap.daily_bias.sign > 0
    assert snap is not None
    assert snap.daily_bias in {
        DailyBiasState.REVERSAL,
        DailyBiasState.MIXED,
        DailyBiasState.BEARISH,
        DailyBiasState.STRONG_BEARISH,
    }
    assert "NEW_YORK" in snap.session_transition_state
    assert snap.previous_session is SessionName.LONDON
    assert snap.previous_session_bias is not None and snap.previous_session_bias.sign > 0


def test_state_evolves_chronologically_and_never_uses_future_candles() -> None:
    """Replaying a prefix must reproduce the same snapshots the full run produced."""
    full = DailyMarketBiasAgent()
    price = 2000.0
    inputs = []
    for index in range(90):
        per_bar = 0.5 if index < 60 else -0.8
        session = SessionName.ASIA if index < 30 else SessionName.LONDON
        inputs.append(
            bias_inputs(
                index,
                close=price + per_bar,
                open_=price,
                session=session,
                ema_fast=price,
                ema_slow=price - per_bar,
                session_trend="up" if per_bar > 0 else "down",
            )
        )
        price += per_bar
    snapshots = [full.observe(one) for one in inputs]
    prefix = DailyMarketBiasAgent()
    for index in range(45):
        assert prefix.observe(inputs[index]) == snapshots[index]
    # The snapshot at candle 44 cannot know about the bearish turn at candle 60.
    assert snapshots[44].daily_bias.sign >= 0
    assert all(s.timestamp < snapshots[60].timestamp for s in snapshots[:60])


def test_a_replayed_or_older_candle_cannot_rewind_the_day() -> None:
    agent = DailyMarketBiasAgent()
    first = agent.observe(bias_inputs(0, close=2001, open_=2000, session=SessionName.ASIA))
    second = agent.observe(bias_inputs(1, close=2002, open_=2001, session=SessionName.ASIA))
    again = agent.observe(bias_inputs(0, close=1990, open_=2000, session=SessionName.ASIA))
    assert again == second
    assert first.timestamp < second.timestamp


def test_session_transition_records_completed_labels_in_order() -> None:
    snap = drift_day(DailyMarketBiasAgent(), per_bar=0.6, bars=90)
    assert snap.current_session is SessionName.NEW_YORK
    assert len(snap.transitions) == 2
    assert snap.transitions[0].startswith("ASIA_")
    assert snap.transitions[1].startswith("LONDON_")
    assert snap.session_transition_state.split("->")[-1].startswith("NEW_YORK_")
    assert snap.sessions["asia"].complete is True
    assert snap.sessions["london"].complete is True
    assert snap.sessions["new_york"].complete is False


def test_session_reset_starts_a_fresh_session_score() -> None:
    agent = DailyMarketBiasAgent()
    price = 2000.0
    for index in range(40):
        agent.observe(
            bias_inputs(
                index,
                close=price + 0.8,
                open_=price,
                session=SessionName.ASIA,
                ema_fast=price + 1,
                ema_slow=price - 2,
                session_trend="up",
            )
        )
        price += 0.8
    asia = agent.current("XAUUSD", "M5")
    assert asia.session_bias.sign > 0
    first_london = agent.observe(
        bias_inputs(
            40,
            close=price - 0.5,
            open_=price,
            session=SessionName.LONDON,
            ema_fast=price,
            ema_slow=price + 1,
            session_trend="down",
        )
    )
    assert first_london.sessions["london"].bars == 1
    assert first_london.session_score < asia.session_score
    assert first_london.asia_bias is asia.session_bias


def test_day_reset_keeps_previous_day_bias_and_clears_transitions() -> None:
    agent = DailyMarketBiasAgent()
    monday = drift_day(agent, per_bar=0.6, bars=60)
    assert monday.transitions
    tuesday = agent.observe(
        bias_inputs(
            500,
            close=2100.5,
            open_=2100,
            session=SessionName.ASIA,
            market_date="2026-01-06",
        )
    )
    assert tuesday.market_date == "2026-01-06"
    assert tuesday.transitions == ()
    assert tuesday.previous_day_bias is monday.daily_bias
    assert tuesday.day_open == 2100.0
    assert tuesday.sessions.keys() == {"asia"}


def test_overbought_rsi_and_an_opposing_sweep_raise_reversal_risk() -> None:
    snap = drift_day(
        DailyMarketBiasAgent(),
        per_bar=0.6,
        bars=60,
        sweep={"direction": "down", "level_type": "previous_day_high"},
    )
    assert snap.reversal_risk in {ReversalRisk.MEDIUM, ReversalRisk.HIGH}


def test_no_session_is_hard_coded_as_best() -> None:
    """A strong Asia and a flat London leave Asia as the best session so far."""
    agent = DailyMarketBiasAgent()
    price = 2000.0
    for index in range(60):
        per_bar = 0.9 if index < 30 else 0.0
        session = SessionName.ASIA if index < 30 else SessionName.LONDON
        snap = agent.observe(
            bias_inputs(
                index,
                close=price + per_bar,
                open_=price,
                session=session,
                ema_fast=price + per_bar,
                ema_slow=price - 3 * per_bar,
                rsi=62 if per_bar else 50,
                session_trend="up" if per_bar else "flat",
                regime={
                    "regime": "trending" if per_bar else "ranging",
                    "volatility_state": "normal",
                },
            )
        )
        price += per_bar
    assert snap.best_session_so_far is SessionName.ASIA
    assert snap.best_direction_so_far is Direction.BUY


def test_state_survives_a_restart_through_the_store(tmp_path) -> None:
    agent = DailyMarketBiasAgent()
    before = drift_day(agent, per_bar=0.6, bars=60)
    store = DailyBiasStateStore(tmp_path / "bias.json")
    store.save(agent.state_dict())

    restored = DailyMarketBiasAgent()
    assert restored.restore(store.load()) == 1
    assert restored.current("XAUUSD", "M5") == before
    # Continuing after the restart matches continuing without it.
    nxt = bias_inputs(
        61, close=2040.6, open_=2040.0, session=SessionName.LONDON, session_trend="up"
    )
    assert restored.observe(nxt) == agent.observe(nxt)


def test_feature_context_only_carries_values_known_at_that_time() -> None:
    agent = DailyMarketBiasAgent()
    price = 2000.0
    for index in range(40):
        session = SessionName.ASIA if index < 20 else SessionName.LONDON
        snap = agent.observe(
            bias_inputs(
                index,
                close=price + 0.6,
                open_=price,
                session=session,
                ema_fast=price + 0.6,
                ema_slow=price - 2,
                session_trend="up",
            )
        )
        price += 0.6
    context = snap.as_feature_context()
    assert context["current_session"] == "london"
    assert context["daily_bias_at_entry"] == snap.daily_bias.value
    assert context["new_york_bias"] == "neutral"  # New York has not happened yet
    assert context["session_transition_state"].startswith("ASIA_")
    assert "->LONDON_" in context["session_transition_state"]

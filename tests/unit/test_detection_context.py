"""Volume-profile and volatility context on a detection (9B, §19).

The property that matters is **no hindsight**: a detection's context describes the moment it
fired and nothing after it. That is easy to state and easy to break -- the obvious
implementation recomputes a profile when something reads it, and a profile recomputed later is
a fuller one. So the test here is the strongest form available: feed one engine a prefix and
another the whole week, and require the detections they share to be byte-identical.

Everything else follows from that. The context is attached in the engine rather than in the
agents, so every detection from one candle carries the same figures, and an agent stays a pure
function of ``(window, ctx)`` -- which is what the replay/live parity contract rests on.
"""

from __future__ import annotations

import pytest

from aureon.config import AureonConfig
from aureon.config.symbol_tuning import VOLATILITY_BANDS_VERSION
from aureon.engine.analysis_engine import AnalysisEngine
from aureon.engine.market_context import DETECTION_SCOPE
from aureon.models.enums import Timeframe
from main_observer import default_agents
from tests.conftest import ACCOUNT_SCOPE, MARKET_TZ, cross_agent

CONFIG = AureonConfig()


def engine_for(symbol: str = "XAUUSD") -> AnalysisEngine:
    return AnalysisEngine(
        default_agents(CONFIG, symbol=symbol),
        account_scope=ACCOUNT_SCOPE,
        market_tz=MARKET_TZ,
    )


# ── No hindsight ──────────────────────────────────────────────────────────────


def test_a_later_candle_never_changes_an_earlier_detection(candles) -> None:
    """The §19 rule, as a byte comparison.

    A prefix run and a full run must agree on every detection the prefix produced. If the
    context were recomputed on read -- or built from the whole window rather than from what
    had closed -- the full run's copies would carry fuller profiles and this would fail.
    """
    prefix = engine_for().feed(candles[:700])
    whole = engine_for().feed(candles)
    assert prefix, "the prefix produced no detections, so nothing was proven"

    by_id = {d.detection_id: d for d in whole}
    for detection in prefix:
        later = by_id.get(detection.detection_id)
        assert later is not None, f"{detection.detection_id} vanished in the longer run"
        assert detection.model_dump(mode="json") == later.model_dump(mode="json")


def test_a_much_louder_later_candle_does_not_move_a_stored_poc(candles) -> None:
    """The specific hindsight the phase names: volume that arrives afterwards.

    The later candles are amplified a hundredfold, which would dominate any profile that
    included them. The detections from before them must not notice.
    """
    cut = 700
    before = engine_for().feed(candles[:cut])
    louder = [
        candle.model_copy(update={"tick_volume": candle.tick_volume * 100})
        for candle in candles[cut : cut + 200]
    ]
    after = engine_for().feed([*candles[:cut], *louder])

    by_id = {d.detection_id: d for d in after}
    for detection in before:
        assert detection.model_dump(mode="json") == by_id[
            detection.detection_id
        ].model_dump(mode="json")


# ── What is attached ──────────────────────────────────────────────────────────


def test_every_detection_after_asia_has_traded_carries_a_profile_reference(candles) -> None:
    produced = engine_for().feed(candles[:700])
    with_ref = [d for d in produced if d.volume_profile_ref is not None]
    assert with_ref, "no detection carried a profile reference"
    sample = with_ref[0]
    assert sample.volume_profile_ref.scope == DETECTION_SCOPE
    assert sample.volume_profile_ref.poc_price is not None
    assert sample.volume_profile_ref.price_vs_va in {"above", "inside", "below"}


def test_a_detection_before_asia_has_traded_carries_no_reference(candles) -> None:
    """Absent context is a gap a reader can see; an empty profile looks like a measurement.

    The fixture's week opens at 01:00 market time and Asia opens at 02:00, so the first hour
    of the first broker day is the window where there is genuinely nothing to refer to. The
    assertion is written against each detection's own market time rather than an index, so it
    keeps testing the property if the fixture's start ever moves.
    """
    from datetime import time

    from aureon.config.sessions import SESSION_WINDOWS
    from aureon.models.enums import SessionName

    asia_open: time = SESSION_WINDOWS[SessionName.ASIA].start
    produced = engine_for().feed(candles[:120])
    assert produced, "the opening candles produced no detections"

    first_day = produced[0].detected_at.market_date
    before_asia = [
        d
        for d in produced
        if d.detected_at.market_date == first_day
        and d.detected_at.market.time() <= asia_open
    ]
    assert before_asia, "the fixture no longer starts before Asia; the case is untested"
    assert all(d.volume_profile_ref is None for d in before_asia)
    # And once Asia has traded, the same run does carry references.
    assert any(d.volume_profile_ref is not None for d in produced)


def test_every_detection_carries_volatility_and_its_bands_version(candles) -> None:
    produced = engine_for().feed(candles[:700])
    assert all(d.volatility is not None for d in produced)
    warmed = [d for d in produced if d.volatility.atr_14 is not None]
    assert warmed, "no detection had a warmed-up ATR"
    assert warmed[0].volatility.atr_points > 0
    labelled = [d for d in produced if d.volatility.regime is not None]
    assert labelled, "no detection carried a regime"
    assert all(d.volatility.bands_version == VOLATILITY_BANDS_VERSION for d in labelled)


def test_detections_from_one_candle_share_one_context(candles) -> None:
    """Computed once per candle, so two agents firing together cannot disagree."""
    produced = engine_for().feed(candles[:900])
    by_candle: dict[str, list] = {}
    for detection in produced:
        by_candle.setdefault(detection.detected_at.utc.isoformat(), []).append(detection)
    shared = [group for group in by_candle.values() if len(group) > 1]
    assert shared, "no candle produced two detections, so nothing was compared"
    for group in shared:
        refs = {
            None if d.volume_profile_ref is None else d.volume_profile_ref.poc_price
            for d in group
        }
        assert len(refs) == 1
        assert len({d.volatility.atr_14 for d in group}) == 1


def test_the_reference_says_where_THIS_detections_price_stood(candles) -> None:
    """The one part that is per detection: two prices, one profile, two answers."""
    produced = engine_for().feed(candles[:900])
    answers = {
        d.volume_profile_ref.price_vs_va
        for d in produced
        if d.volume_profile_ref is not None
    }
    assert len(answers) > 1, f"every detection reported the same position: {answers}"


# ── The version bump (§12) ────────────────────────────────────────────────────


def test_every_agents_version_moved_so_the_populations_are_separable() -> None:
    """9B changed what a detection document contains, for every agent.

    The id is a hash over the agent_version (§12), so bumping it means a pre-9B detection
    and a post-9B one over the same candle have different ids. Without that, two documents
    of different shapes would share an id and nothing would distinguish them.
    """
    versions = {a.agent_name: a.agent_version for a in default_agents(CONFIG)}
    assert versions["ema_cross"] == "2.1.0"
    assert versions["liquidity"] == "1.1.0"
    assert versions["breakout"] == "1.1.0"
    assert versions["wick"] == "1.1.0"
    # Not only the four the phase listed: these gained the same fields.
    assert versions["rsi"] == "1.1.0"
    assert versions["session_trend"] == "1.1.0"


def test_the_id_changes_with_the_version(candles) -> None:
    """Stated as the consequence rather than as a hash: same candle, different version."""
    from aureon.models.identity import detection_id

    old = detection_id(
        account_scope=ACCOUNT_SCOPE,
        symbol="XAUUSD",
        timeframe="M5",
        candle_close=candles[100].close_time,
        agent_name="ema_cross",
        agent_version="2.0.0",
        event_key="bullish",
    )
    new = detection_id(
        account_scope=ACCOUNT_SCOPE,
        symbol="XAUUSD",
        timeframe="M5",
        candle_close=candles[100].close_time,
        agent_name="ema_cross",
        agent_version="2.1.0",
        event_key="bullish",
    )
    assert old != new


# ── Both symbols ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("symbol", ["XAUUSD", "XAGUSD"])
def test_context_is_attached_for_either_symbol(
    request: pytest.FixtureRequest, symbol: str
) -> None:
    """Each symbol's own bin width and tick, from its own tuning entry (9B)."""
    week = request.getfixturevalue(
        "candles" if symbol == "XAUUSD" else "silver_candles"
    )[:700]
    produced = engine_for(symbol).feed(week)
    assert produced, f"{symbol} produced no detections"
    refs = [d.volume_profile_ref for d in produced if d.volume_profile_ref is not None]
    assert refs, f"{symbol} carried no profile reference"
    assert all(d.volatility is not None for d in produced)

    tracker = engine_for(symbol)  # a fresh engine, to read the tuning it would use
    tracker.feed(week[:300])
    context = tracker.context_tracker(symbol, Timeframe.M5)
    assert context is not None
    assert context.tuning.point == (0.01 if symbol == "XAUUSD" else 0.001)
    assert context.tuning.volume_bin_points == (10.0 if symbol == "XAUUSD" else 2.0)


def test_a_single_agent_engine_still_gets_context(candles) -> None:
    """The attachment is the engine's, so it does not depend on the roster's shape."""
    produced = AnalysisEngine(
        [cross_agent()], account_scope=ACCOUNT_SCOPE, market_tz=MARKET_TZ
    ).feed(candles[:700])
    assert produced
    assert all(d.volatility is not None for d in produced)

"""One observer, two symbols, and nothing of one leaking into the other (9A).

``AnalysisEngine`` already keys its windows and cross counters by ``(symbol, timeframe)``,
so a single engine would keep the right *history* for two symbols. What it cannot keep is
the right *parameters*: its agents carry one symbol's thresholds, and those are not
dimensionless — ``min_penetration_points = 5`` is $0.05 on gold and 0.625 of a tick on
silver (decision 143).

So the properties worth testing are all about separation:

* each symbol's detections carry **its own** agent parameters;
* the ids are disjoint even for candles that closed at the same instant, because §12 puts
  the symbol in the id;
* each symbol is evaluated under **its own** rule and its own tick;
* a candle for a symbol this process does not observe is refused rather than measured with
  whatever roster happened to be first.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aureon.config import AureonConfig
from aureon.engine.analysis_engine import AnalysisEngine
from aureon.engine.symbol_engines import SymbolEngines
from aureon.models.market import Candle
from aureon.outbox.local_outbox import LocalOutbox
from aureon.outbox.outbox_worker import OutboxWorker
from aureon.services.observer_state import ObserverState
from aureon.storage.detection_repository import DetectionRepository
from main_observer import Observer, default_agents
from tests.conftest import MARKET_TZ, InMemoryFirestore

GOLD, SILVER = "XAUUSD", "XAGUSD"


def two_symbol_config(tmp_path: Path) -> AureonConfig:
    return AureonConfig.from_env(
        env={
            "AUREON_ACCOUNT_SCOPE": "primary",
            "AUREON_MARKET_TZ": MARKET_TZ,
            "AUREON_SYMBOLS": f"{GOLD},{SILVER}",
            "AUREON_TIMEFRAMES": "M5",
            "AUREON_EVAL_RULES": f"{GOLD}:XAU_OUTCOME_V2,{SILVER}:XAG_OUTCOME_V1",
            "AUREON_OUTBOX_PATH": str(tmp_path / "outbox.db"),
            "AUREON_OBSERVER_STATE_PATH": str(tmp_path / "observer_state.json"),
        }
    )


def interleaved(gold: list[Candle], silver: list[Candle], count: int) -> list[Candle]:
    """Both symbols' candles in time order, as a live poll would see them."""
    both = gold[:count] + silver[:count]
    return sorted(both, key=lambda c: (c.open_time.utc, c.symbol))


def build(tmp_path: Path, candles: list[Candle]):
    from tests.conftest import FakeLiveProvider

    config = two_symbol_config(tmp_path)
    provider = FakeLiveProvider(candles)
    outbox = LocalOutbox(config.outbox_path)
    firestore = InMemoryFirestore()
    worker = OutboxWorker(outbox, DetectionRepository(firestore).upsert_payload)
    observer = Observer(
        config,
        provider,
        outbox=outbox,
        worker=worker,
        state=ObserverState(config.observer_state_path),
    )
    return observer, provider, outbox, firestore


# ── The engines ──────────────────────────────────────────────────────────────


def test_each_symbol_gets_its_own_engine_and_its_own_level_tracker(tmp_path) -> None:
    observer, _provider, outbox, _store = build(tmp_path, [])
    try:
        assert set(observer.engines.symbols) == {GOLD, SILVER}
        gold_engine = observer.engines.for_symbol(GOLD)
        silver_engine = observer.engines.for_symbol(SILVER)
        assert gold_engine is not silver_engine

        def liquidity(engine: AnalysisEngine):
            return next(a for a in engine.agents if a.agent_name == "liquidity")

        def breakout(engine: AnalysisEngine):
            return next(a for a in engine.agents if a.agent_name == "breakout")

        # §15/§17: liquidity and breakout share ONE tracker WITHIN a symbol...
        assert liquidity(gold_engine).level_tracker is breakout(gold_engine).level_tracker
        # ...and never across symbols, where "where a level is" is a different question.
        assert (
            liquidity(gold_engine).level_tracker
            is not liquidity(silver_engine).level_tracker
        )
    finally:
        outbox.close()


def test_a_symbol_this_process_does_not_observe_is_refused() -> None:
    """Loudly. The quiet alternative is measuring it with whatever roster was first."""
    from tests.conftest import cross_agent

    engines = SymbolEngines(
        {
            GOLD: AnalysisEngine(
                [cross_agent()], account_scope="primary", market_tz=MARKET_TZ
            )
        }
    )
    with pytest.raises(KeyError, match="no analysis engine for EURUSD"):
        engines.for_symbol("EURUSD")
    assert GOLD in engines and "EURUSD" not in engines


def test_an_empty_engine_map_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one engine"):
        SymbolEngines({})


def test_one_roster_cannot_be_handed_to_two_symbols(tmp_path) -> None:
    """It would share the agent instances AND their LevelTracker between instruments --
    the bug the whole split exists to prevent, so it raises rather than doing it."""
    from tests.conftest import FakeLiveProvider, cross_agent

    config = two_symbol_config(tmp_path)
    outbox = LocalOutbox(config.outbox_path)
    try:
        with pytest.raises(ValueError, match="one agent roster cannot serve 2 symbols"):
            Observer(
                config,
                FakeLiveProvider([]),
                outbox=outbox,
                worker=OutboxWorker(outbox, lambda payload: None),
                state=ObserverState(config.observer_state_path),
                agents=[cross_agent()],
            )
    finally:
        outbox.close()


# ── Detections keep their symbols apart ──────────────────────────────────────


@pytest.fixture(scope="module")
def both_symbols_replayed(candles_module, silver_module):
    """Both fixtures through their own engines, as the observer wires them."""
    config = AureonConfig(
        symbols=(GOLD, SILVER),
        evaluation_rules={GOLD: "XAU_OUTCOME_V2", SILVER: "XAG_OUTCOME_V1"},
    )
    engines = SymbolEngines(
        {
            symbol: AnalysisEngine(
                default_agents(config, symbol=symbol),
                account_scope="primary",
                market_tz=MARKET_TZ,
            )
            for symbol in config.symbols
        }
    )
    return engines.feed(interleaved(candles_module, silver_module, 700))


@pytest.fixture(scope="module")
def candles_module():
    from aureon.data.historical_provider import HistoricalDataProvider
    from tests.conftest import FIXTURE_CSV

    return HistoricalDataProvider(FIXTURE_CSV, market_tz=MARKET_TZ).candles


@pytest.fixture(scope="module")
def silver_module():
    from aureon.data.historical_provider import HistoricalDataProvider
    from tests.conftest import SILVER_FIXTURE_CSV

    return HistoricalDataProvider(
        SILVER_FIXTURE_CSV, market_tz=MARKET_TZ, symbol=SILVER
    ).candles


def test_both_symbols_produce_detections(both_symbols_replayed) -> None:
    produced = {d.symbol for d in both_symbols_replayed}
    assert produced == {GOLD, SILVER}


def test_each_symbols_detections_carry_its_own_thresholds(both_symbols_replayed) -> None:
    """The point of the split, checked on the stored snapshot rather than on the agent."""
    for symbol, penetration, point in ((GOLD, 5.0, 0.01), (SILVER, 1.0, 0.001)):
        sweeps = [
            d
            for d in both_symbols_replayed
            if d.symbol == symbol and d.agent_name == "liquidity"
        ]
        assert sweeps, f"{symbol} produced no sweeps to check"
        assert {d.agent_params_snapshot["min_penetration_points"] for d in sweeps} == {
            penetration
        }
        assert {d.agent_params_snapshot["point"] for d in sweeps} == {point}


def test_ids_are_disjoint_even_at_the_same_candle_time(both_symbols_replayed) -> None:
    """§12 puts the symbol in the id, so two symbols closing the same bar cannot collide.

    The fixtures share a calendar deliberately, so this is not a theoretical case: every
    candle time in one has a twin in the other.
    """
    by_symbol: dict[str, set[str]] = {GOLD: set(), SILVER: set()}
    for detection in both_symbols_replayed:
        by_symbol[detection.symbol].add(detection.detection_id)

    assert by_symbol[GOLD] and by_symbol[SILVER]
    assert not (by_symbol[GOLD] & by_symbol[SILVER])

    # And the case is real rather than incidental: the same instant, the same agent,
    # both symbols.
    shared = {
        (d.detected_at.utc, d.agent_name, d.event_key)
        for d in both_symbols_replayed
        if d.symbol == GOLD
    } & {
        (d.detected_at.utc, d.agent_name, d.event_key)
        for d in both_symbols_replayed
        if d.symbol == SILVER
    }
    assert shared, "the fixtures never coincide, so this test proves nothing"


def test_the_two_symbols_are_not_the_same_series(both_symbols_replayed) -> None:
    """A rescaled copy of gold would make every multi-symbol test pass for a reason that
    has nothing to do with the code."""
    counts = {
        symbol: sum(1 for d in both_symbols_replayed if d.symbol == symbol)
        for symbol in (GOLD, SILVER)
    }
    assert counts[GOLD] != counts[SILVER]


# ── The observer, end to end ─────────────────────────────────────────────────


def test_the_observer_routes_a_mixed_stream_by_symbol(
    tmp_path, candles_module, silver_module
) -> None:
    stream = interleaved(candles_module, silver_module, 400)
    observer, provider, outbox, store = build(tmp_path, stream)
    try:
        for candle in stream:
            provider.advance_to_close_of(candle)
            observer.market_engine.poll_once()
        observer.worker.flush()

        stored = [
            payload
            for path, payload in store.docs.items()
            if "_detections/" in path
        ]
        assert {payload["symbol"] for payload in stored} == {GOLD, SILVER}, (
            "both symbols reached Firestore"
        )
        assert outbox.pending_count() == 0

        # Each engine saw only its OWN candles: its window holds its symbol and nothing
        # else, which is what a shared engine with a shared roster could not promise.
        timeframe = stream[0].timeframe
        for symbol in (GOLD, SILVER):
            engine = observer.engines.for_symbol(symbol)
            assert engine.window_length(symbol, timeframe) > 0
            other = SILVER if symbol == GOLD else GOLD
            assert engine.window_length(other, timeframe) == 0
    finally:
        observer.worker.stop()
        outbox.close()


def test_each_symbol_is_evaluated_under_its_own_rule(tmp_path) -> None:
    """Silver's $0.10 is 100 points at its own tick and 10 at gold's, so the tracker needs
    the symbol's rule AND its point (decision 138, again)."""
    from aureon.evaluation.outcome_tracker import OutcomeTracker
    from aureon.evaluation.rules import get_rule

    config = two_symbol_config(tmp_path)
    trackers = {
        symbol: OutcomeTracker(
            get_rule(config.rule_id_for(symbol)),
            market_tz=MARKET_TZ,
            point=0.01 if symbol == GOLD else 0.001,
        )
        for symbol in config.symbols
    }
    assert trackers[GOLD].rule.rule_id == "XAU_OUTCOME_V2"
    assert trackers[SILVER].rule.rule_id == "XAG_OUTCOME_V1"
    # $3 on gold is 300 points; $0.10 on silver is 100 -- not 10, which is what gold's
    # tick would have made it.
    assert trackers[GOLD].rule.thresholds_in_points(0.01)[0] == 300.0
    assert trackers[SILVER].rule.thresholds_in_points(0.001)[0] == 100.0

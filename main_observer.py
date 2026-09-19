#!/usr/bin/env python3
"""The observer process (§75).

Watches the market, produces detections, and writes them durably. It does not
trade, and it holds no broker handle -- a boundary test forbids the observation
packages from importing ``aureon.execution`` at all.

## Startup order, and why it is this order (§75)

1. **connect** to the data provider;
2. **load the cursor** from ``observer_state.json`` -- the last candle fully
   processed;
3. **flush the outbox** *before* observing anything new. Detections queued by the
   previous run are older than anything about to be produced, and delivering them
   first keeps Firestore's ordering sensible. It also proves the connection works
   before a backlog is added to;
4. **backfill** the closed candles between the cursor and now, in chronological
   order, through the same engine the live loop uses -- so candles missed during a
   restart are treated identically to candles seen live;
5. **resume live** polling.

Shutdown flushes the outbox again, so a clean stop leaves nothing queued. An unclean
stop is safe too: the queue is durable and step 3 will drain it next boot.
"""

from __future__ import annotations

import logging
import signal
import sys
from datetime import timedelta
from types import FrameType

from aureon.agents.base_agent import BaseAgent
from aureon.agents.breakout_agent import BreakoutAgent
from aureon.agents.ema_cross_agent import EmaCrossAgent
from aureon.agents.liquidity_agent import LiquidityAgent
from aureon.agents.rsi_agent import RsiAgent
from aureon.agents.session_trend_agent import SessionTrendAgent, summary_from_detection
from aureon.agents.wick_agent import WickAgent
from aureon.config import AureonConfig
from aureon.data.base_provider import BaseMarketDataProvider
from aureon.engine.analysis_engine import AnalysisEngine
from aureon.engine.levels import LevelTracker
from aureon.engine.market_engine import MarketEngine
from aureon.evaluation.outcome_tracker import OutcomeTracker
from aureon.evaluation.rules import get_rule
from aureon.models.detection import Detection
from aureon.models.enums import MarketState, Timeframe
from aureon.models.market import Candle
from aureon.models.system import SymbolState, SystemState
from aureon.outbox.local_outbox import LocalOutbox
from aureon.outbox.outbox_worker import OutboxWorker
from aureon.services.heartbeat_service import HeartbeatService
from aureon.services.market_state_service import MarketStateService
from aureon.services.observer_state import ObserverState
from aureon.storage import paths

log = logging.getLogger("aureon.observer")

# How far back to backfill when there is no cursor at all (a first-ever run).
COLD_START_BARS = 500


class Observer:
    """Wires the observation pipeline together."""

    def __init__(
        self,
        config: AureonConfig,
        provider: BaseMarketDataProvider,
        *,
        outbox: LocalOutbox,
        worker: OutboxWorker,
        state: ObserverState,
        market_state: MarketStateService | None = None,
        state_repository: object | None = None,
        session_repository: object | None = None,
        evaluation_repository: object | None = None,
        outcome_tracker: OutcomeTracker | None = None,
        heartbeat: HeartbeatService | None = None,
        agents: list[BaseAgent] | None = None,
    ) -> None:
        self.config = config
        self.provider = provider
        self.outbox = outbox
        self.worker = worker
        self.state = state
        self.market_state = market_state
        self.state_repository = state_repository
        self.session_repository = session_repository
        self.evaluation_repository = evaluation_repository
        # Decision 50: evaluation runs IN the observer process.
        self.outcome_tracker = outcome_tracker
        self.heartbeat = heartbeat

        self.engine = AnalysisEngine(
            agents or default_agents(config),
            account_scope=config.account_scope,
            market_tz=config.market_tz,
        )
        self.market_engine = MarketEngine(
            provider,
            self.engine,
            symbols=config.symbols,
            timeframes=config.timeframes,
            on_detections=self._on_detections,
            on_candle_close=self._on_candle_close,
        )
        self._last_candles: dict[tuple[str, Timeframe], Candle] = {}

    # ── Detection sink ────────────────────────────────────────────────────────

    def _on_detections(self, detections: list[Detection]) -> None:
        """Queue detections durably. Local write first, delivery later (§83)."""
        inserted = self.outbox.enqueue_many(detections)
        log.info(
            "queued %d detection(s) (%d new): %s",
            len(detections),
            inserted,
            ", ".join(f"{d.agent_name}/{d.event_key}" for d in detections),
        )
        self._write_session_summaries(detections)
        self._track_outcomes(detections)

    def _track_outcomes(self, detections: list[Detection]) -> None:
        """Begin evaluating new detections, and close any horizons they end (§22)."""
        if self.outcome_tracker is None:
            return
        try:
            for detection in detections:
                # Notify BEFORE tracking: a reversing detection closes the
                # opposite_cross horizon of earlier ones, and cannot close its own.
                self._persist_evaluations(self.outcome_tracker.on_detection(detection))
                started = self.outcome_tracker.track(detection)
                if started is not None:
                    self._persist_evaluations([started])
        except Exception:  # noqa: BLE001 - evaluation must never stop observation
            log.exception("outcome tracking failed for a detection batch")

    def _advance_evaluations(self, candle: Candle) -> None:
        if self.outcome_tracker is None:
            return
        try:
            self._persist_evaluations(self.outcome_tracker.on_closed_candle(candle))
            # Finished evaluations are dropped so a long-running observer does not
            # accumulate them; they are already persisted.
            self.outcome_tracker.release_closed()
        except Exception:  # noqa: BLE001 - see above
            log.exception("advancing evaluations failed at %s", candle.open_time.utc)

    def _persist_evaluations(self, evaluations: list) -> None:
        """Upsert evaluations. Like session summaries, these bypass the outbox.

        An evaluation is a pure function of the detection and the candles that
        followed, so a lost write is regenerated by re-running the backfill rather
        than lost -- unlike a detection, which can never be re-derived once its
        candle has passed out of the provider's history.
        """
        if self.evaluation_repository is None or not evaluations:
            return
        for evaluation in evaluations:
            try:
                self.evaluation_repository.upsert(evaluation)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 - recoverable; see the docstring
                log.exception(
                    "failed to write evaluation for %s", evaluation.detection_id
                )

    def _write_session_summaries(self, detections: list[Detection]) -> None:
        """Write ``sessions/`` for any completed session (§18).

        The agent stays pure and only supplies the numbers; the write happens here,
        because a Firestore write from an agent would break both the purity contract
        and the rule that only repositories touch Firestore (CLAUDE.md).

        Unlike detections, these do not go through the outbox: a session summary is
        derived entirely from candles the observer will re-process on restart, so a
        lost write is regenerated rather than lost. Failing loudly here would stop
        observation for something recoverable.
        """
        if self.session_repository is None:
            return
        for detection in detections:
            if detection.agent_name != SessionTrendAgent.agent_name:
                continue
            try:
                summary = summary_from_detection(detection)
                self.session_repository.upsert(summary)  # type: ignore[attr-defined]
                log.info(
                    "wrote session %s (%s, %d candles)",
                    summary.session_id,
                    summary.trend,
                    summary.candle_count,
                )
            except Exception:  # noqa: BLE001 - recoverable; see the docstring
                log.exception("failed to write session summary for %s", detection.event_key)

    def _on_candle_close(self, candle: Candle) -> None:
        """Advance the cursor, evaluations and state on every candle close."""
        key = (candle.symbol, candle.timeframe)
        self._last_candles[key] = candle
        self._advance_evaluations(candle)
        # Saved per candle, not per poll: a crash between two candles must not
        # re-process the earlier one.
        self.state.set_and_save(candle.symbol, candle.timeframe, candle.open_time.utc)
        self._write_system_state(force=True)

    def _write_system_state(self, *, force: bool = False) -> None:
        if self.state_repository is None:
            return
        symbols: list[SymbolState] = []
        for symbol in self.config.symbols:
            for timeframe in self.config.timeframes:
                candle = self._last_candles.get((symbol, timeframe))
                result = (
                    self.market_state.state_for(symbol)
                    if self.market_state is not None
                    else None
                )
                symbols.append(
                    SymbolState(
                        symbol=symbol,
                        timeframe=timeframe,
                        market_state=result.state if result else MarketState.UNKNOWN,
                        last_closed_candle_time=candle.open_time.utc if candle else None,
                    )
                )
        try:
            self.state_repository.write(  # type: ignore[attr-defined]
                SystemState(symbols=tuple(symbols)), force=force
            )
        except Exception:  # noqa: BLE001 - state reporting must not stop observation
            log.exception("system_state write failed")

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def startup(self) -> int:
        """Run the §75 sequence. Returns the number of backfilled detections."""
        log.info(
            "observer starting: scope=%s tz=%s",
            self.config.account_scope,
            self.config.market_tz,
        )
        self.provider.connect()

        # Step 3: deliver what the previous run queued, before adding to it.
        flushed = self.worker.flush()
        if flushed:
            log.info("flushed %d queued detection(s) from a previous run", flushed)
        pending = self.outbox.pending_count()
        if pending:
            log.warning("%d detection(s) still undelivered; the worker will retry", pending)

        # Step 4: backfill the gap, chronologically, through the live engine.
        backfilled = 0
        for symbol in self.config.symbols:
            for timeframe in self.config.timeframes:
                backfilled += self._backfill(symbol, timeframe)

        if self.heartbeat is not None:
            self.heartbeat.start()
        self.worker.start()
        return backfilled

    def _backfill(self, symbol: str, timeframe: Timeframe) -> int:
        """Re-warm the engine, then process everything after the cursor.

        The subtlety that makes this more than a simple range fetch: the engine is
        FRESH after a restart, and its indicators are recursive, so a window that
        begins at the cursor computes different EMAs than the uninterrupted run
        would have. Starting there manufactures spurious crossovers for roughly a
        window's worth of candles -- a restart would silently produce detections
        that never existed, and miss ones that did.

        So the fetch reaches back a full ``window_size`` BEFORE the cursor. Those
        warm-up candles are fed through the engine to rebuild its window but their
        detections are discarded: they were already handled by the previous run.
        Only candles strictly after the cursor are emitted.
        """
        cursor = self.state.get(symbol, timeframe)
        now = self.provider.now_utc()
        bar = timedelta(minutes=timeframe.minutes)

        if cursor is None:
            start = now - bar * COLD_START_BARS
            log.info("no cursor for %s %s; cold start from %s", symbol, timeframe.value, start)
        else:
            # Reach back a full window so the fresh engine's indicators match what
            # the uninterrupted run would have computed.
            start = cursor - bar * self.engine.window_size
            log.info(
                "backfilling %s %s from %s (cursor %s, re-warming %d bars)",
                symbol,
                timeframe.value,
                start.isoformat(),
                cursor.isoformat(),
                self.engine.window_size,
            )

        candles = self.provider.get_closed_candles(symbol, timeframe, start, now)
        if not candles:
            # Nothing to backfill, but the live loop still needs the cursor so it
            # does not fall back to its lookback window and re-derive old candles.
            if cursor is not None:
                self.market_engine.seed_cursor(symbol, timeframe, cursor)
            return 0

        produced = 0
        warmed = 0
        for candle in candles:
            detections = self.engine.on_closed_candle(candle)
            is_warmup = cursor is not None and candle.open_time.utc <= cursor
            if is_warmup:
                # Window rebuilt; these detections belong to the previous run. (Even
                # if they were re-queued the outbox would drop them as duplicates --
                # this just avoids the pointless churn.)
                warmed += 1
                continue
            if detections:
                self._on_detections(detections)
                produced += len(detections)
            self._last_candles[(symbol, timeframe)] = candle
            self.state.set(symbol, timeframe, candle.open_time.utc)
        self.state.save()
        if warmed:
            log.info("re-warmed the engine with %d candle(s) at or before the cursor", warmed)

        # Hand the live loop the same cursor, so it does not re-fetch the backfill.
        self.market_engine.seed_cursor(symbol, timeframe, candles[-1].open_time.utc)
        log.info(
            "backfilled %d candle(s) for %s %s, %d detection(s)",
            len(candles),
            symbol,
            timeframe.value,
            produced,
        )
        return produced

    def run(self) -> None:
        """Serve until stopped."""
        self.startup()
        log.info("observer live")
        try:
            self.market_engine.run(poll_seconds=1.0)
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        """Stop cleanly, leaving nothing queued if Firestore is reachable."""
        log.info("observer shutting down")
        self.market_engine.stop()
        if self.heartbeat is not None:
            self.heartbeat.stop()
        self.worker.stop()
        remaining = self.worker.flush()
        if remaining:
            log.info("delivered %d detection(s) during shutdown", remaining)
        still = self.outbox.pending_count()
        if still:
            # Not an error: the queue is durable and the next boot drains it first.
            log.warning("%d detection(s) remain queued; they will be delivered next start", still)
        self.state.save()
        self.provider.close()


def default_agents(config: AureonConfig, *, point: float = 0.01) -> list[BaseAgent]:
    """The full Part A + Part B roster.

    The liquidity and breakout agents are handed **the same** ``LevelTracker``
    instance (§15, §17). Two trackers would be two implementations of where a level
    is, and the agents would disagree about the same bar -- one reporting a sweep of a
    high the other never considered a high.

    One timeframe is assumed for the level and session agents, because their window
    sizes are derived from it; a multi-timeframe deployment builds one roster per
    timeframe.
    """
    timeframe = config.timeframes[0]
    levels = LevelTracker()
    return [
        EmaCrossAgent(),
        RsiAgent(),
        SessionTrendAgent(timeframe=timeframe, point=point),
        WickAgent(point=point),
        LiquidityAgent(timeframe=timeframe, point=point, level_tracker=levels),
        BreakoutAgent(timeframe=timeframe, point=point, level_tracker=levels),
    ]


def build_observer(config: AureonConfig) -> Observer:
    """Assemble a live observer from configuration."""
    from aureon.data.mt5_provider import MT5DataProvider
    from aureon.storage.detection_repository import DetectionRepository
    from aureon.storage.evaluation_repository import EvaluationRepository
    from aureon.storage.firebase_service import get_client
    from aureon.storage.session_repository import SessionRepository
    from aureon.storage.system_state_repository import (
        HeartbeatRepository,
        SystemStateRepository,
    )

    provider = MT5DataProvider(
        market_tz=config.market_tz,
        login=config.mt5_login,
        password=config.mt5_password,
        server=config.mt5_server,
        terminal_path=config.mt5_terminal_path,
    )
    client = get_client(
        project_id=config.firebase_project_id, emulator_host=config.firestore_emulator_host
    )
    detections = DetectionRepository(client)
    outbox = LocalOutbox(config.outbox_path)
    worker = OutboxWorker(outbox, detections.upsert_payload)
    heartbeat_repo = HeartbeatRepository(
        client, min_interval_seconds=config.state_heartbeat_seconds
    )

    return Observer(
        config,
        provider,
        outbox=outbox,
        worker=worker,
        state=ObserverState(config.observer_state_path),
        market_state=MarketStateService(provider),
        state_repository=SystemStateRepository(
            client, min_interval_seconds=config.state_heartbeat_seconds
        ),
        session_repository=SessionRepository(client),
        evaluation_repository=EvaluationRepository(client),
        outcome_tracker=OutcomeTracker(
            get_rule(config.evaluation_rule_id), market_tz=config.market_tz
        ),
        heartbeat=HeartbeatService(heartbeat_repo, paths.SERVICE_OBSERVER),
    )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    config = AureonConfig.from_env()
    observer = build_observer(config)

    def handle(signum: int, _frame: FrameType | None) -> None:
        log.info("received signal %s", signum)
        observer.market_engine.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, handle)

    observer.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())

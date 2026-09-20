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
from datetime import datetime, timedelta
from types import FrameType

from aureon.agents.base_agent import BaseAgent
from aureon.agents.breakout_agent import BreakoutAgent
from aureon.agents.ema_cross_agent import EmaCrossAgent
from aureon.agents.liquidity_agent import LiquidityAgent
from aureon.agents.rsi_agent import RsiAgent
from aureon.agents.session_trend_agent import SessionTrendAgent, summary_from_detection
from aureon.agents.wick_agent import WickAgent
from aureon.config import AureonConfig
from aureon.config.sessions import session_for
from aureon.config.symbol_tuning import require_tuning
from aureon.data.base_provider import BaseMarketDataProvider
from aureon.data.live_candle_archive import LiveCandleArchive
from aureon.engine.analysis_engine import AnalysisEngine
from aureon.engine.levels import LevelTracker
from aureon.engine.market_engine import MarketEngine
from aureon.engine.symbol_engines import SymbolEngines
from aureon.evaluation.outcome_tracker import OutcomeTracker
from aureon.evaluation.rules import get_rule
from aureon.models.detection import Detection
from aureon.models.enums import MarketState, Timeframe
from aureon.models.market import Candle
from aureon.models.system import SymbolState, SystemState
from aureon.outbox.local_outbox import LocalOutbox
from aureon.outbox.outbox_worker import OutboxWorker
from aureon.services.heartbeat_service import HeartbeatService
from aureon.services.market_snapshot import MarketSnapshot
from aureon.services.market_state_service import MarketStateService
from aureon.services.observer_state import ObserverState
from aureon.storage import paths

log = logging.getLogger("aureon.observer")

# How far back to backfill when there is no cursor at all (a first-ever run).
COLD_START_BARS = 500

#: How many times ``_reach_back`` may double its request before giving up. Eight
#: doublings turns one window into 256, which clears any weekend, holiday or broker
#: outage; bounded so a provider that keeps returning the same short history cannot
#: spin here forever.
MAX_REACH_BACK_DOUBLINGS = 8


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
        symbol_repository: object | None = None,
        evaluation_repository: object | None = None,
        outcome_trackers: dict[str, OutcomeTracker] | None = None,
        heartbeat: HeartbeatService | None = None,
        candle_archive: LiveCandleArchive | None = None,
        agents: list[BaseAgent] | dict[str, list[BaseAgent]] | None = None,
    ) -> None:
        self.config = config
        self.provider = provider
        self.outbox = outbox
        self.worker = worker
        self.state = state
        self.market_state = market_state
        self.state_repository = state_repository
        self.session_repository = session_repository
        # Discord may not call the broker, so the observer publishes symbol metadata for
        # it to read (decision 79).
        self.symbol_repository = symbol_repository
        self.evaluation_repository = evaluation_repository
        # Decision 50: evaluation runs IN the observer process. One tracker per symbol,
        # because an outcome rule's thresholds are in the instrument's own money and its
        # conversion to points needs that symbol's tick (decision 141).
        self.outcome_trackers: dict[str, OutcomeTracker] = dict(outcome_trackers or {})
        self.heartbeat = heartbeat

        # One engine, one roster and one LevelTracker per symbol. See SymbolEngines for
        # why a single shared engine cannot do this: it would keep the right history and
        # the wrong thresholds.
        self.engines = SymbolEngines(
            {symbol: AnalysisEngine(
                roster,
                account_scope=config.account_scope,
                market_tz=config.market_tz,
            )
                for symbol, roster in _rosters(config, agents).items()}
        )
        self.market_engine = MarketEngine(
            provider,
            self.engines,
            symbols=config.symbols,
            timeframes=config.timeframes,
            on_detections=self._on_detections,
            on_candle_close=self._on_candle_close,
        )
        self._last_candles: dict[tuple[str, Timeframe], Candle] = {}
        # One snapshot per symbol/timeframe, fed by every detection and candle so
        # /status reports the SAME indicator values that were stored, never a
        # recomputation that could disagree with them (§59, §66).
        self._snapshots: dict[tuple[str, Timeframe], MarketSnapshot] = {}
        self.candle_archive = candle_archive

    # ── Detection sink ────────────────────────────────────────────────────────

    def _snapshot(self, symbol: str, timeframe: Timeframe) -> MarketSnapshot:
        return self._snapshots.setdefault(
            (symbol, timeframe), MarketSnapshot(symbol=symbol)
        )

    def _on_detections(self, detections: list[Detection]) -> None:
        """Queue detections durably. Local write first, delivery later (§83)."""
        inserted = self.outbox.enqueue_many(detections)
        log.info(
            "queued %d detection(s) (%d new): %s",
            len(detections),
            inserted,
            ", ".join(f"{d.agent_name}/{d.event_key}" for d in detections),
        )
        for detection in detections:
            self._snapshot(detection.symbol, detection.timeframe).observe(detection)
        self._write_session_summaries(detections)
        self._track_outcomes(detections)

    def _track_outcomes(self, detections: list[Detection]) -> None:
        """Begin evaluating new detections, and close any horizons they end (§22).

        Routed by the detection's own symbol: each symbol's tracker holds that symbol's
        rule and tick, so handing gold's detection to silver's tracker would measure it
        against $0.10 thresholds converted with the wrong point.
        """
        try:
            for detection in detections:
                tracker = self.outcome_trackers.get(detection.symbol)
                if tracker is None:
                    continue
                # Notify BEFORE tracking: a reversing detection closes the
                # opposite_cross horizon of earlier ones, and cannot close its own.
                self._persist_evaluations(tracker.on_detection(detection))
                started = tracker.track(detection)
                if started is not None:
                    self._persist_evaluations([started])
        except Exception:  # noqa: BLE001 - evaluation must never stop observation
            log.exception("outcome tracking failed for a detection batch")

    def _advance_evaluations(self, candle: Candle) -> None:
        tracker = self.outcome_trackers.get(candle.symbol)
        if tracker is None:
            return
        try:
            self._persist_evaluations(tracker.on_closed_candle(candle))
            # Finished evaluations are dropped so a long-running observer does not
            # accumulate them; they are already persisted.
            tracker.release_closed()
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
        # Session extremes come from candles, not detections: a session has a high
        # whether or not anything detected anything.
        self._snapshot(candle.symbol, candle.timeframe).observe_candle(
            high=candle.high,
            low=candle.low,
            session=session_for(candle.open_time.market),
        )
        if self.candle_archive is not None:
            # §82: record what the broker actually served, so a live-vs-replay
            # comparison can replay these exact bytes rather than a fresh fetch.
            try:
                self.candle_archive.add(candle)
            except Exception:  # noqa: BLE001 - archiving must never stop observing
                log.exception("could not archive %s", candle.open_time.utc)
        self._advance_evaluations(candle)
        # Saved per candle, not per poll: a crash between two candles must not
        # re-process the earlier one.
        self.state.set_and_save(candle.symbol, candle.timeframe, candle.open_time.utc)
        self._write_system_state(force=True)

    def _latest_quote(self, symbol: str):
        """The provider's current quote, or None if it cannot be read.

        Failure is not an error here: state reporting must never stop observation, and a
        missing quote correctly makes Discord re-prompt rather than proceed.
        """
        try:
            return self.provider.get_quote(symbol)
        except Exception:  # noqa: BLE001 - diagnostic, never critical
            log.debug("could not read a quote for %s", symbol, exc_info=True)
            return None

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
                        # Published so Discord can show bid/ask and judge staleness without
                        # calling the broker (decision 80). One snapshot, overwritten in
                        # place -- not a tick stream.
                        last_quote=self._latest_quote(symbol),
                        # Read from the engine rather than recounted here: the engine
                        # already resets them on the market clock, and a second tally
                        # would eventually disagree with the first.
                        **self.engines.for_symbol(symbol)
                        .cross_counts(symbol, timeframe)
                        .as_state(),
                        **self._snapshot(symbol, timeframe).as_state(),
                        **self._market_context(symbol, timeframe),
                    )
                )
        try:
            self.state_repository.write(  # type: ignore[attr-defined]
                SystemState(symbols=tuple(symbols)), force=force
            )
        except Exception:  # noqa: BLE001 - state reporting must not stop observation
            log.exception("system_state write failed")

    def _market_context(self, symbol: str, timeframe) -> dict[str, object]:
        """This symbol's profile summaries and volatility, for the state document (9B).

        Read from the engine's tracker rather than recomputed here, for the same reason the
        cross counts are: a second computation would eventually disagree with the one the
        detections were stamped with, and the panel would then describe a market the stored
        detections never saw.
        """
        from aureon.models.profile import ProfileSummary

        tracker = self.engines.for_symbol(symbol).context_tracker(symbol, timeframe)
        if tracker is None:
            # No candle has closed for this symbol yet. An absent block is honest; an empty
            # one would render as a profile that found nothing.
            return {}
        return {
            "volume_profile": {
                scope: ProfileSummary.of(profile)
                for scope, profile in tracker.profiles().items()
            },
            "volatility": tracker.volatility(),
        }

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

        self._publish_symbol_specs()

        # Step 4: backfill the gap, chronologically, through the live engine.
        backfilled = 0
        for symbol in self.config.symbols:
            for timeframe in self.config.timeframes:
                backfilled += self._backfill(symbol, timeframe)

        if self.heartbeat is not None:
            self.heartbeat.start()
        self.worker.start()
        return backfilled

    def _publish_symbol_specs(self) -> None:
        """Publish broker symbol metadata for Discord (decision 79).

        Best-effort: a failure here costs Discord a good error message, not an unsafe
        trade -- the execution guard re-reads the live symbol regardless (§56).
        """
        if self.symbol_repository is None:
            return
        for symbol in self.config.symbols:
            try:
                info = self.provider.symbol_info(symbol)
                self.symbol_repository.publish(info)  # type: ignore[attr-defined]
                log.info(
                    "published symbol spec for %s (step %s, modes %s)",
                    symbol,
                    info.volume_step,
                    [m.value for m in info.filling_modes] or "none reported",
                )
            except Exception:  # noqa: BLE001 - see the docstring
                log.exception("could not publish a symbol spec for %s", symbol)

    def _reach_back(
        self, symbol: str, timeframe: Timeframe, *, cursor: datetime, now: datetime
    ) -> list[Candle]:
        """Fetch enough history to re-warm the engine: a count of CANDLES, not a span
        of time.

        This distinction is the whole function. ``window_size`` bars of history is
        ``window_size * timeframe`` minutes only while the market never closes. Reach
        back 151 bars in wall-clock time from the first candle after a weekend and the
        request lands inside the 49-hour gap: the provider returns ~70 candles, the
        fresh engine's EMAs never converge, and the restart silently loses detections
        the uninterrupted run produced.

        That is not hypothetical -- it is the bug that ``test_restart_produces_no_
        duplicates_and_no_gap[1500]`` caught. A time-based reach-back survived the
        9/21 pair by coincidence (64 bars of reach happened to fit in the six hours of
        candles after the gap) and broke the moment the warm-up grew to 150.

        So: widen the request until it actually yields ``window_size`` candles at or
        before the cursor, or until widening stops returning anything new -- which is
        how a genuinely short history (a brand-new symbol) terminates.
        """
        bar = timedelta(minutes=timeframe.minutes)
        needed = self.engines.for_symbol(symbol).window_size
        span = bar * needed
        candles: list[Candle] = []

        for _attempt in range(MAX_REACH_BACK_DOUBLINGS):
            candles = self.provider.get_closed_candles(
                symbol, timeframe, cursor - span, now
            )
            warmup = sum(1 for c in candles if c.open_time.utc <= cursor)
            if warmup >= needed:
                return candles
            # Deliberately NOT stopping when the count failed to grow. "Asked for
            # earlier candles and got none" means either a market closure or the start
            # of history, and from here those look identical -- an early exit on the
            # first unchanged count is what left the reach-back starved inside the
            # weekend gap. Widening to the cap costs a handful of reads, once, at
            # startup; guessing wrong costs a silently missed detection.
            span *= 2

        # A genuinely short history (a new symbol) lands here. Not a failure, but said
        # out loud: the first detections after this restart may differ from an
        # uninterrupted run's, and silence would make that look like an agent bug.
        log.warning(
            "%s %s: only %d of %d warm-up candles available after reaching back to %s; "
            "early detections may differ from an uninterrupted run",
            symbol,
            timeframe.value,
            sum(1 for c in candles if c.open_time.utc <= cursor),
            needed,
            (cursor - span).isoformat(),
        )
        return candles

    def _backfill(self, symbol: str, timeframe: Timeframe) -> int:
        """Re-warm the engine, then process everything after the cursor.

        The subtlety that makes this more than a simple range fetch: the engine is
        FRESH after a restart, and its indicators are recursive, so a window that
        begins at the cursor computes different EMAs than the uninterrupted run
        would have. Starting there manufactures spurious crossovers for roughly a
        window's worth of candles -- a restart would silently produce detections
        that never existed, and miss ones that did.

        So the fetch reaches back a full ``window_size`` worth of CANDLES before the
        cursor (see ``_reach_back`` for why candles and not minutes). Those warm-up
        candles are fed through the engine to rebuild its window but their detections
        are discarded: they were already handled by the previous run. Only candles
        strictly after the cursor are emitted.
        """
        cursor = self.state.get(symbol, timeframe)
        now = self.provider.now_utc()
        bar = timedelta(minutes=timeframe.minutes)

        if cursor is None:
            start = now - bar * COLD_START_BARS
            log.info("no cursor for %s %s; cold start from %s", symbol, timeframe.value, start)
            candles = self.provider.get_closed_candles(symbol, timeframe, start, now)
        else:
            log.info(
                "backfilling %s %s (cursor %s, re-warming %d bars)",
                symbol,
                timeframe.value,
                cursor.isoformat(),
                self.engines.for_symbol(symbol).window_size,
            )
            candles = self._reach_back(symbol, timeframe, cursor=cursor, now=now)
        if not candles:
            # Nothing to backfill, but the live loop still needs the cursor so it
            # does not fall back to its lookback window and re-derive old candles.
            if cursor is not None:
                self.market_engine.seed_cursor(symbol, timeframe, cursor)
            return 0

        produced = 0
        warmed = 0
        engine = self.engines.for_symbol(symbol)
        for candle in candles:
            detections = engine.on_closed_candle(candle)
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
        if self.candle_archive is not None:
            # Flushed here, not per candle: rewriting a parquet file every five minutes
            # would cost more than the archive is worth. A crash therefore loses the
            # current day's tail, which the merge on the next flush recovers.
            try:
                for path in self.candle_archive.flush_all():
                    log.info("flushed live candles to %s", path)
            except Exception:  # noqa: BLE001
                log.exception("could not flush the live-candle archive")
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


def _rosters(
    config: AureonConfig, agents: list[BaseAgent] | dict[str, list[BaseAgent]] | None
) -> dict[str, list[BaseAgent]]:
    """One roster per configured symbol.

    An explicit ``agents`` list is accepted only for a SINGLE-symbol process. Handing one
    list to two engines would share the agent instances and their ``LevelTracker`` between
    two instruments, which is the bug this whole split exists to prevent -- so it raises
    rather than doing it quietly. A dict is the multi-symbol override.
    """
    if isinstance(agents, dict):
        missing = [s for s in config.symbols if s not in agents]
        if missing:
            raise ValueError(f"no agent roster supplied for {', '.join(missing)}")
        return {symbol: agents[symbol] for symbol in config.symbols}
    if agents is not None:
        if len(config.symbols) > 1:
            raise ValueError(
                "one agent roster cannot serve "
                f"{len(config.symbols)} symbols: its thresholds and its LevelTracker "
                "belong to one instrument. Pass {symbol: [agents]}."
            )
        return {config.symbols[0]: agents}
    return {symbol: default_agents(config, symbol=symbol) for symbol in config.symbols}


def default_agents(
    config: AureonConfig,
    *,
    symbol: str | None = None,
    point: float | None = None,
) -> list[BaseAgent]:
    """The full Part A + Part B roster.

    The liquidity and breakout agents are handed **the same** ``LevelTracker``
    instance (§15, §17). Two trackers would be two implementations of where a level
    is, and the agents would disagree about the same bar -- one reporting a sweep of a
    high the other never considered a high.

    One timeframe is assumed for the level and session agents, because their window
    sizes are derived from it; a multi-timeframe deployment builds one roster per
    timeframe.

    **Refuses a symbol with no tuning entry** (P-6). ``point`` defaults to the tuning
    table's value rather than to gold's 0.01; a caller that has read ``symbol_info.point``
    from the broker passes it and it wins, because that is the tick the symbol actually
    has.

    ``symbol`` says which symbol the roster is FOR, and defaults to the first configured
    one. With two symbols each gets its own roster (9A): the thresholds are not
    dimensionless, so a shared roster would measure silver with gold's numbers.
    """
    timeframe = config.timeframes[0]
    wanted = symbol or config.symbols[0]
    levels = LevelTracker()
    # EVERY configured symbol, not just the one the roster is built for: the observer
    # watches them all, and finding out about the third one three hours in is finding
    # out too late.
    for configured in config.symbols:
        require_tuning(configured)
    # Per-symbol, because the level and wick thresholds are NOT dimensionless:
    # min_penetration_points = 5 is $0.05 on gold and something else entirely on a
    # symbol with a different tick and a different daily range (D-15).
    tuning = require_tuning(wanted, point=point)
    if not tuning.is_default:
        log.info(
            "%s runs tuned agent parameters: %s",
            wanted,
            ", ".join(tuning.overridden),
        )
    return [
        EmaCrossAgent(fast_period=config.ema_fast, slow_period=config.ema_slow),
        RsiAgent(),
        SessionTrendAgent(
            timeframe=timeframe, point=tuning.point, flat_points=tuning.flat_points
        ),
        WickAgent(
            point=tuning.point,
            min_wick_range_ratio=tuning.min_wick_range_ratio,
            min_wick_body_ratio=tuning.min_wick_body_ratio,
            max_close_position=tuning.max_close_position,
            min_range_points=tuning.min_range_points,
        ),
        LiquidityAgent(
            timeframe=timeframe,
            point=tuning.point,
            level_tracker=levels,
            min_penetration_points=tuning.min_penetration_points,
            min_rejection_fraction=tuning.min_rejection_fraction,
        ),
        BreakoutAgent(
            timeframe=timeframe,
            point=tuning.point,
            level_tracker=levels,
            min_close_beyond_points=tuning.min_close_beyond_points,
        ),
    ]


def build_observer(config: AureonConfig) -> Observer:
    """Assemble a live observer from configuration."""
    from aureon.data.mt5_provider import MT5DataProvider
    from aureon.storage.detection_repository import DetectionRepository
    from aureon.storage.evaluation_repository import EvaluationRepository
    from aureon.storage.firebase_service import get_client
    from aureon.storage.session_repository import SessionRepository
    from aureon.storage.symbol_repository import SymbolRepository
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
        symbol_repository=SymbolRepository(client),
        evaluation_repository=EvaluationRepository(client),
        outcome_trackers={
            # Each symbol's own rule AND its own tick. The tick matters as much as the
            # rule: XAG_OUTCOME_V1's $0.10 is 100 points at silver's 0.001 and would be
            # 10 at gold's 0.01, so a shared point would measure silver's thresholds ten
            # times too small (decision 138, again).
            symbol: OutcomeTracker(
                get_rule(config.rule_id_for(symbol)),
                market_tz=config.market_tz,
                point=require_tuning(symbol).point,
            )
            for symbol in config.symbols
        },
        candle_archive=LiveCandleArchive(),
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

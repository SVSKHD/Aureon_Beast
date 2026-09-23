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

## The weekend (11B)

The process does not exit at the close and is not restarted at the open. Exiting would
turn a predictable weekly event into a restart, and a restart is the one moment this
service can lose its cursor or leave its outbox undrained. So at a confirmed close it
parks the candle loop, flushes everything durable, slows the heartbeat and keeps
polling -- once a minute -- for the market to come back. It wakes itself before the
open, so the backfill is done by the time the first real candle closes.

What stays running matters as much as what stops: the heartbeat (a heartbeat that
stopped would be indistinguishable from a process that died over the weekend), the
provider connection (reconnecting at 22:00 Sunday is the worst moment to discover the
terminal is logged out), and the ops register.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta

from aureon.agents.base_agent import BaseAgent
from aureon.agents.breakout_agent import BreakoutAgent
from aureon.agents.ema_cross_agent import EmaCrossAgent
from aureon.agents.liquidity_agent import LiquidityAgent
from aureon.agents.rsi_agent import RsiAgent
from aureon.agents.session_trend_agent import SessionTrendAgent, summary_from_detection
from aureon.agents.wick_agent import WickAgent
from aureon.config import AureonConfig
from aureon.config.sessions import session_for
from aureon.config.symbol_tuning import require_tuning, tuning_for
from aureon.data.base_provider import BaseMarketDataProvider
from aureon.data.live_candle_archive import LiveCandleArchive
from aureon.engine.analysis_engine import AnalysisEngine
from aureon.engine.levels import LevelTracker
from aureon.engine.market_engine import MarketEngine
from aureon.engine.symbol_engines import SymbolEngines
from aureon.evaluation.outcome_tracker import OutcomeTracker
from aureon.evaluation.rules import get_rule
from aureon.models.base import to_utc, utc_now
from aureon.models.detection import Detection
from aureon.models.enums import (
    MarketState,
    MtfAlignment,
    SetupState,
    Timeframe,
    TrendBias,
)
from aureon.models.market import Candle
from aureon.models.system import SymbolState, SystemState
from aureon.outbox.local_outbox import LocalOutbox
from aureon.outbox.outbox_worker import OutboxWorker
from aureon.services.alert_watcher import AlertWatcher, build_snapshot, minutes_since
from aureon.services.heartbeat_service import HeartbeatService
from aureon.services.market_snapshot import MarketSnapshot
from aureon.services.market_state_service import MarketStateService, WeeklySchedule
from aureon.services.observer_state import ObserverState
from aureon.services.ops_events import OpsRegister
from aureon.services.shutdown import flushed, install_handlers
from aureon.services.sleep_cycle import SleepCycle, SleepGate
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
        alert_repository: object | None = None,
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
        # 9C: the observer answers price alerts because it is the process with quotes --
        # Discord may not call the broker, and a second MT5 connection polling levels
        # would double the terminal's load for no new information.
        self.alerts = (
            AlertWatcher(alert_repository) if alert_repository is not None else None
        )

        #: 11B. The schedule comes from the market-state service when there is one, so the
        #: two cannot disagree about when the open is -- a second WeeklySchedule would be a
        #: second place for the weekly boundary to be wrong.
        self.sleep = SleepCycle(
            schedule=(
                market_state.schedule if market_state is not None else WeeklySchedule()
            ),
            close_confirm_seconds=config.close_confirm_seconds,
            preopen_seconds=config.preopen_seconds,
            sleep_heartbeat_seconds=config.sleep_heartbeat_seconds,
            sleep_poll_seconds=config.sleep_poll_seconds,
        )
        #: This poll's market states, read once and shared by the sleep decision and the
        #: ops report. Two independent reads per poll would cost two provider round trips
        #: per symbol and could disagree with each other.
        self._states: dict[str, MarketState] = {}
        #: 11D. Optional: an observer without one still observes and still attaches the MTF
        #: context from its own buffer; it just does not cache the day for anybody else.
        self.market_days: object | None = None
        #: This broker day's M5 bars per stream, and which day that is, so a rollover is
        #: visible without a clock: the arrival of a bar on a later date IS the rollover.
        self._day_bars: dict[tuple[str, Timeframe], list[Candle]] = {}
        self._day_of: dict[tuple[str, Timeframe], str] = {}
        #: Whether this day's buffer began at its ROLLOVER rather than at process start.
        #:
        #: A process that starts at 14:00 has the afternoon's bars and none of the morning's,
        #: and writing that as a complete day would be worse than writing nothing: a D1 bar
        #: aggregated from it would have the wrong open, the wrong low and a plausible shape.
        #: So the first day a process sees is written with ``complete=False``, and only a day
        #: whose first bar arrived as a rollover is complete.
        self._day_clean: dict[tuple[str, Timeframe], bool] = {}

        # One engine, one roster and one LevelTracker per symbol. See SymbolEngines for
        # why a single shared engine cannot do this: it would keep the right history and
        # the wrong thresholds.
        self.engines = SymbolEngines(
            {symbol: AnalysisEngine(
                roster,
                account_scope=config.account_scope,
                market_tz=config.market_tz,
                # 11B: a bar straddling the weekly close is not a bar -- its range IS the
                # weekend gap. The engine refuses to analyse one and records why.
                gap_guard=self.sleep.schedule.close_spanned_by,
                # 11D: a longer M5 tail than the analysis window, for the higher timeframes.
                mtf_bars=config.mtf_m5_bars,
                mtf_periods=(config.ema_fast, config.ema_slow),
            )
                for symbol, roster in _rosters(config, agents).items()}
        )
        #: 11B. The three steps of a sleep decision, shared with the other services so
        #: "what counts as closed" has one implementation rather than four.
        self.gate = SleepGate(
            cycle=self.sleep,
            states=self._market_states,
            clock=self._now_or_none,
            on_sleep=self._on_market_close,
            on_wake=self._on_market_open,
            service=paths.SERVICE_OBSERVER,
        )
        self.market_engine = MarketEngine(
            provider,
            self.engines,
            symbols=config.symbols,
            timeframes=config.timeframes,
            on_detections=self._on_detections,
            on_candle_close=self._on_candle_close,
            on_analysis=self._on_analysis,
            on_poll=self._on_poll,
            before_poll=self.gate.tick,
            parked=lambda: self.gate.parked,
            pace=self.gate.pace,
        )
        self._last_candles: dict[tuple[str, Timeframe], Candle] = {}
        #: 12 T-7. One setup engine per symbol, or none at all: an observer with no Firestore
        #: client tracks no setups and still observes. Set by ``build_observer``.
        self.setups: dict[str, object] = {}
        #: One per symbol, built on first use. See ``_levels_for``.
        self._level_trackers: dict[str, object] = {}
        #: What the setup engines read as "now". Set from each candle's close.
        self._setup_clock = utc_now()
        #: 12 T-7. One per symbol, beside the setup engine. Each holds an ``OutcomeTracker`` over
        #: the SAME frozen rule the detections use, so there is one definition of an outcome.
        self.setup_evaluators: dict[str, object] = {}
        #: 12 T-9. One reference book per symbol, holding the past outcomes a new setup is
        #: measured against. Empty without a Firestore client, like the engines above.
        self.setup_references: dict[str, object] = {}
        #: 11A F-3. Read once per connection; see ``_account_mode``.
        self._cached_account_mode: object | None = None
        #: 11A F-15. Optional: an observer without one still observes, it just says nothing
        #: about its own health. Set by ``main`` when a Firestore client exists.
        self.ops: object | None = None
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
        if self._spans_a_close(candle):
            self._skip_candle(candle)
            return
        key = (candle.symbol, candle.timeframe)
        self._last_candles[key] = candle
        # Session extremes come from candles, not detections: a session has a high
        # whether or not anything detected anything.
        self._snapshot(candle.symbol, candle.timeframe).observe_candle(
            high=candle.high,
            low=candle.low,
            open=candle.open,
            close=candle.close,
            session=session_for(candle.open_time.market),
        )
        if self.candle_archive is not None:
            # §82: record what the broker actually served, so a live-vs-replay
            # comparison can replay these exact bytes rather than a fresh fetch.
            try:
                self.candle_archive.add(candle)
            except Exception:  # noqa: BLE001 - archiving must never stop observing
                log.exception("could not archive %s", candle.open_time.utc)
        self._cache_market_day(candle)
        self._advance_evaluations(candle)
        # Saved per candle, not per poll: a crash between two candles must not
        # re-process the earlier one.
        self.state.set_and_save(candle.symbol, candle.timeframe, candle.open_time.utc)
        if self.alerts is not None:
            # On the candle clock, so expiry runs on the same clock as everything else the
            # observer records and needs no second scheduler (9C).
            self.alerts.expire()
        self._write_system_state(force=True)

    # ── Setups (12, T-7) ──────────────────────────────────────────────────────

    def _on_analysis(self, candle: Candle, detections: list[Detection]) -> None:
        """Run the setup engine over this closed candle.

        Called for every candle, including the ones that produced nothing: an expiry, an
        invalidation and a proximity all happen with no detection, and a hook that only fired on
        detections would miss exactly those.

        Wrapped whole, because a setup engine that raised would otherwise take the candle loop
        with it -- and the candle loop is the one thing in this process that must not stop. A
        setup is context; a missed candle is a hole in the archive and in the parity check.
        """
        engine = self.setups.get(candle.symbol)
        if engine is None or candle.timeframe is not Timeframe.M5:
            return
        # The engine's clock, set from the DATA rather than from the wall clock, so a replay
        # stamps what the live session stamped. See ``_wire_setups``.
        self._setup_clock = candle.close_time
        try:
            events = engine.on_closed_candle(self._setup_inputs(candle, detections))
        except Exception:  # noqa: BLE001 - see the docstring
            log.exception("the setup engine raised on %s", candle.open_time.utc)
            return
        self._evaluate_setups(candle, engine, events)

    def _evaluate_setups(self, candle: Candle, engine: object, events: list) -> None:
        """Advance the open measurements, then start one for anything that just confirmed.

        That order matters and is the same one the detection evaluations use: a setup confirming
        on this candle is measured FROM this candle's close, so it must not also be advanced by
        it -- a horizon that counted its own opening bar would report an excursion the market had
        not made yet.
        """
        evaluator = self.setup_evaluators.get(candle.symbol)
        if evaluator is None:
            return
        try:
            evaluator.on_closed_candle(candle)
        except Exception:  # noqa: BLE001 - measurement must not stop observation
            log.exception("the setup evaluator raised on %s", candle.open_time.utc)
        tracked = getattr(engine, "tracked", {})
        for event in events:
            if event.to_state is not SetupState.CONFIRMED:
                continue
            setup = tracked.get(event.setup_id)
            if setup is None:
                continue
            try:
                evaluator.on_confirmed(setup, candle)
            except Exception:  # noqa: BLE001
                log.exception("could not begin evaluating setup %s", event.setup_id)

    def _setup_inputs(self, candle: Candle, detections: list[Detection]):
        """Assemble what the setup engine needs from what this candle already computed.

        Everything here is a read of state the observer holds anyway -- the analysis engine's
        indicators, its levels, its MTF context, and the snapshot that feeds ``/status``. Nothing
        is recomputed, and nothing is fetched: a second source for any of these numbers would be
        a second answer, and the two would disagree on the candle where it mattered.
        """
        from aureon.services.setup_engine import SetupInputs

        analysis = self.engines.for_symbol(candle.symbol)
        read = analysis.indicator_read(candle.symbol, candle.timeframe)
        snapshot = self._snapshot(candle.symbol, candle.timeframe)
        mtf = analysis.mtf_context(candle.symbol, candle.timeframe)
        # The alignment on the CONTEXT is direction-free: a detection's alignment depends on its
        # own direction (11D), and a setup has its own. MIXED is the honest default here.
        alignment = MtfAlignment.MIXED
        profile = getattr(snapshot, "volume_profile", None)
        volatility = getattr(snapshot, "volatility", None)
        current_trend = self._trend_read(candle.symbol, candle.timeframe)
        return SetupInputs(
            candle=candle,
            market_date=candle.open_time.market_date,
            session=session_for(candle.open_time.market),
            detections=tuple(detections),
            levels=self._levels_for(candle),
            atr=read.atr,
            ema_fast=read.ema_fast,
            ema_slow=read.ema_slow,
            previous_ema_fast=read.previous_ema_fast,
            previous_ema_slow=read.previous_ema_slow,
            rsi=read.rsi,
            previous_rsi=read.previous_rsi,
            trend=(
                getattr(current_trend, "bias", None)
                if current_trend is not None
                else TrendBias.SIDEWAYS
            ),
            mtf_alignment=alignment if mtf is None else alignment,
            volatility_regime=getattr(volatility, "regime", None),
            price_vs_va=getattr(profile, "price_vs_va", None),
            value_area_high=getattr(profile, "value_area_high", None),
            value_area_low=getattr(profile, "value_area_low", None),
            poc_price=getattr(profile, "poc_price", None),
            median_tick_volume=read.median_tick_volume,
        )

    def _levels_for(self, candle: Candle):
        """The levels the agents are using for this symbol, derived from the SAME window.

        ``LevelTracker.levels_for`` is a pure function of its window and its swing strength, so a
        tracker built here from the symbol's own tuning derives exactly the levels the agents
        derived -- provided it is handed the engine's window rather than a fresh fetch. A
        different window would give different pivots, and a setup could then anchor to a level no
        detection ever saw.
        """
        from aureon.engine.levels import Levels, LevelTracker

        tracker = self._level_trackers.get(candle.symbol)
        if tracker is None:
            tracker = LevelTracker()
            self._level_trackers[candle.symbol] = tracker
        frame = self.engines.for_symbol(candle.symbol).window(
            candle.symbol, candle.timeframe
        )
        if frame is None or len(frame) < 2:
            return Levels()
        try:
            return tracker.levels_for(frame, self.config.market_tz)
        except Exception:  # noqa: BLE001 - a level derivation must not stop the loop
            log.exception("could not derive levels for %s", candle.symbol)
            return Levels()

    # ── The broker-day cache (11D) ────────────────────────────────────────────

    def _cache_market_day(self, candle: Candle) -> None:
        """Buffer this bar into its broker day, and write the day when it rolls over.

        The rollover is the moment a day will never grow again -- the same moment the parquet
        archive is flushed, and for the same reason. Writing on every candle instead would be
        288 writes a day per symbol for a document nothing reads until the day is done.

        The day in progress is published as an M5 frame with ``complete=False`` for Discord
        charts. Research and MTF readers still request complete frames only, so the live frame
        cannot be mistaken for a finished broker day. This costs one local upsert per closed
        M5 candle and avoids Discord receiving zero bars until day rollover.
        """
        if self.market_days is None or candle.timeframe is not Timeframe.M5:
            return
        key = (candle.symbol, candle.timeframe)
        day = candle.open_time.market_date
        pending = self._day_bars.setdefault(key, [])
        previous = self._day_of.get(key)
        if previous is None:
            # The first bar this process has seen. Its day may already be half over.
            self._day_clean[key] = False
        elif previous != day:
            self._write_market_day(
                candle.symbol, previous, pending, complete=self._day_clean.get(key, False)
            )
            pending = []
            self._day_bars[key] = pending
            self._day_clean[key] = True
        self._day_of[key] = day
        pending.append(candle)
        self._write_live_chart_frame(candle.symbol, day, pending)

    def _write_live_chart_frame(
        self, symbol: str, market_date: str, candles: list[Candle]
    ) -> None:
        """Publish the in-progress M5 frame for Discord charts.

        Finished-day readers still require complete=True. This incomplete frame exists only
        so another local process can render the current session; without it Discord asks
        get_frame for today and receives zero bars until day rollover.
        """
        if not candles or self.market_days is None:
            return
        from aureon.services.market_day_builder import build_frame

        try:
            self.market_days.write_frame(  # type: ignore[union-attr]
                build_frame(
                    symbol,
                    market_date,
                    Timeframe.M5,
                    candles,
                    market_tz=self.config.market_tz,
                    complete=False,
                )
            )
        except Exception:  # noqa: BLE001 - chart cache failure must not stop observation
            log.exception("could not publish live chart frame for %s %s", symbol, market_date)

    def _hydrate_live_chart_day(
        self, symbol: str, timeframe: Timeframe, candles: list[Candle]
    ) -> None:
        """Seed today's chart cache from startup/backfill candles.

        Backfill intentionally bypasses ``_on_candle_close`` for warm-up bars, so without this
        the Discord process sees zero current-day bars after every restart until new live M5
        candles accumulate. Keep the whole current broker-day slice and continue appending to it
        in ``_cache_market_day``.
        """
        if timeframe is not Timeframe.M5 or not candles:
            return
        latest_day = candles[-1].open_time.market_date
        today = [
            candle
            for candle in candles
            if candle.timeframe is Timeframe.M5
            and candle.symbol == symbol
            and candle.open_time.market_date == latest_day
        ]
        if not today:
            return
        key = (symbol, Timeframe.M5)
        # De-duplicate in case the provider returned overlapping reach-back windows.
        by_open = {candle.open_time.utc: candle for candle in today}
        ordered = [by_open[at] for at in sorted(by_open)]
        self._day_bars[key] = ordered
        self._day_of[key] = latest_day
        self._day_clean[key] = False
        self._write_live_chart_frame(symbol, latest_day, ordered)

    def _write_market_day(
        self,
        symbol: str,
        market_date: str,
        candles: list[Candle],
        *,
        complete: bool,
    ) -> None:
        """One broker day: its shape, and its bars at each cached timeframe.

        ``complete`` is the caller's claim that this day was seen from its first bar. A day a
        process joined halfway through is written with it False -- and a False day is skipped
        by ``recent_frames``, so a D1 bar is never aggregated from an afternoon.
        """
        if not candles or self.market_days is None:
            return
        from aureon.models.market_day import CACHED
        from aureon.services.market_day_builder import build_day, build_frame

        try:
            for timeframe in CACHED:
                self.market_days.write_frame(  # type: ignore[union-attr]
                    build_frame(
                        symbol,
                        market_date,
                        timeframe,
                        candles,
                        market_tz=self.config.market_tz,
                        complete=complete,
                    )
                )
            self.market_days.write_day(  # type: ignore[union-attr]
                build_day(
                    symbol,
                    market_date,
                    candles,
                    market_tz=self.config.market_tz,
                    complete=complete,
                )
            )
            log.info(
                "cached %s %s: %d M5 bars across %d timeframes (%s)",
                symbol,
                market_date,
                len(candles),
                len(CACHED),
                "complete" if complete else "PARTIAL — seen from mid-day",
            )
        except Exception:  # noqa: BLE001 - a cache write must never stop observing
            log.exception("could not cache %s %s", symbol, market_date)

    # ── The weekend (11B) ─────────────────────────────────────────────────────

    def _market_states(self) -> dict[str, MarketState]:
        """Every configured symbol's state, read once per poll.

        An observer with no market-state service returns ``{}`` and therefore never
        sleeps: it has no way to tell a closed market from a dead one, and staying awake is
        the answer that cannot turn an outage into silence. A symbol the service raises on
        becomes UNKNOWN for the same reason -- UNKNOWN is a fault, and faults stay awake.
        """
        if self.market_state is None:
            self._states = {}
            return self._states
        states: dict[str, MarketState] = {}
        for symbol in self.config.symbols:
            try:
                states[symbol] = self.market_state.state_for(symbol).state
            except Exception:  # noqa: BLE001
                states[symbol] = MarketState.UNKNOWN
        self._states = states
        return states

    def _on_market_close(self, crossing: object) -> None:
        """Park the loops and leave nothing in flight over the weekend.

        Everything here is work that a two-day pause makes urgent rather than optional. The
        outbox is drained because a detection sitting in it is a detection nobody has seen;
        the candle archive is flushed because its unflushed tail is lost on a crash and the
        weekend is a long time to hold one; the cursor is saved because that is the only
        record of where to resume.
        """
        log.info(
            "market closed; parking until %s",
            getattr(crossing, "next_open", None),
        )
        delivered = self.worker.flush()
        if delivered:
            log.info("delivered %d detection(s) before the close", delivered)
        remaining = self.outbox.pending_count()
        if remaining:
            log.warning(
                "%d detection(s) still queued at the close; the next open drains them",
                remaining,
            )
        if self.candle_archive is not None:
            try:
                for path in self.candle_archive.flush_all():
                    log.info("flushed live candles to %s", path)
            except Exception:  # noqa: BLE001 - archiving must never stop observing
                log.exception("could not flush the live-candle archive at the close")
        self.state.save()
        # 11D: the week's last broker day ends HERE. Waiting for the next bar to announce the
        # rollover would mean waiting until Sunday night -- and a restart over the weekend
        # would lose the buffer and the day with it. A midweek closure is left alone: the
        # market reopens the same day and the buffer is still the same day's.
        if getattr(crossing, "weekly", False):
            self._flush_market_days()
        self._set_heartbeat_cadence()
        # Forced, so /status says CLOSED at once rather than at the next candle -- and
        # there will not BE a next candle for two days.
        self._write_system_state(force=True)

    def _flush_market_days(self) -> None:
        """Write every buffered broker day, because the trading week has ended (11D)."""
        for (symbol, timeframe), candles in list(self._day_bars.items()):
            market_date = self._day_of.get((symbol, timeframe))
            if market_date is None or not candles:
                continue
            self._write_market_day(
                symbol,
                market_date,
                candles,
                complete=self._day_clean.get((symbol, timeframe), False),
            )
            self._day_bars[(symbol, timeframe)] = []
            # Cleared, so the next bar starts a fresh day rather than appending Monday's bars
            # to Friday's -- and NOT marked clean, because the next day this process sees
            # begins wherever the market reopens rather than at a rollover it watched.
            self._day_of.pop((symbol, timeframe), None)
            self._day_clean[(symbol, timeframe)] = False

    def _on_market_open(self, crossing: object) -> None:
        """Come back up, early enough that the first real candle finds us ready."""
        log.info("market opening; resuming observation (%s)", self.sleep.phase.value)
        self._set_heartbeat_cadence()
        self._write_system_state(force=True)

    def _set_heartbeat_cadence(self) -> None:
        """Slow the heartbeat while parked, and restore it on waking.

        Slowed, never stopped: a heartbeat that stopped over the weekend would be
        indistinguishable from a process that died at the close, which is precisely the
        thing the heartbeat exists to tell apart.
        """
        if self.heartbeat is None:
            return
        cadence = self.sleep.heartbeat_seconds(self.config.state_heartbeat_seconds)
        try:
            self.heartbeat.set_interval(cadence)
        except Exception:  # noqa: BLE001 - a cadence change must not stop observing
            log.exception("could not change the heartbeat cadence")

    def _spans_a_close(self, candle: Candle) -> bool:
        """Whether this bar straddles a weekly close (11B).

        The same predicate the analysis engine is given, asked again here rather than
        inferred from "the engine returned no detections" -- a quiet candle and a refused
        one are different things, and a check that could not tell them apart would archive
        and measure the gap bar on every week where nothing happened to cross.
        """
        return (
            self.sleep.schedule.close_spanned_by(candle.open_time.utc, candle.close_time)
            is not None
        )

    def _skip_candle(self, candle: Candle) -> None:
        """Archive a gap-spanning bar, advance past it, and measure nothing from it.

        Archived, because §82 says the archive records what the broker actually SERVED, and
        replay reaching the same guard on the same bytes is the whole point of there being
        one engine. Everything else is skipped: its high and low are the weekend gap, so a
        session extreme taken from it would be a two-day move recorded as five minutes, and
        an outcome measured through it would be an excursion nobody could have traded.

        The cursor still advances -- both the engine's and the durable one -- or the same
        bar would be refetched and refused on every poll for ever.
        """
        log.warning(
            "%s %s bar %s spans the weekly close; not analysed",
            candle.symbol,
            candle.timeframe.value,
            candle.open_time.utc.isoformat(),
        )
        if self.candle_archive is not None:
            try:
                self.candle_archive.add(candle)
            except Exception:  # noqa: BLE001 - archiving must never stop observing
                log.exception("could not archive %s", candle.open_time.utc)
        self.state.set_and_save(candle.symbol, candle.timeframe, candle.open_time.utc)

    def _now_or_none(self) -> datetime:
        """The provider's clock, falling back to the process clock.

        Only ever used for a DISPLAY field. Nothing that is stored as a measurement reads
        it, which is what makes the fallback acceptable: the alternative is a status embed
        that cannot say when the market opens because the terminal was busy.
        """
        try:
            return self.provider.now_utc()
        except Exception:  # noqa: BLE001
            from aureon.models.base import utc_now

            return utc_now()

    def _on_poll(self) -> None:
        """Everything that runs on the ~1s poll clock, in the order that matters.

        Alerts first: a level crossed is a thing a human asked to be told and is worth a
        moment's head start over the system talking about itself.

        Parked, alerts are skipped: there are no new quotes, and firing a level off a
        two-day-old quote would answer a question nobody asked. The ops report still runs,
        because "the outbox is backing up" is true on a Saturday too.
        """
        if not self.sleep.parked:
            self._check_alerts()
        self._report_ops()

    def _report_ops(self) -> None:
        """The conditions only the observer can see (11A, F-15).

        Called on the poll clock, which is also where it belongs: "no candle has closed for two
        intervals" and "this symbol has not ticked for thirty seconds" are both questions about
        elapsed time, and a check that only ran on candle close could not notice that candle
        closes had stopped.

        Every threshold comes from config; the register owns only the once-and-once rule.
        """
        if self.ops is None:
            return
        try:
            now = self.provider.now_utc()
        except Exception:  # noqa: BLE001 - a provider that cannot say the time is stale anyway
            return

        for symbol in self.config.symbols:
            for timeframe in self.config.timeframes:
                open_now = self._market_is_open(symbol)
                candle = self._last_candles.get((symbol, timeframe))
                interval = timeframe.seconds * self.config.ops_observer_stale_intervals
                overdue = (
                    open_now
                    and candle is not None
                    and (now - candle.close_time).total_seconds() > interval
                )
                self._observe_ops(
                    "observer_stale",
                    active=bool(overdue),
                    detail=(
                        f"{symbol} {timeframe.value}: last close "
                        f"{candle.close_time.isoformat()}"
                        if candle is not None
                        else ""
                    ),
                )

            # Per symbol, because silver's feed going quiet says nothing about gold's.
            dark = False
            try:
                last_tick = self.provider.last_tick_time(symbol)
                if open_now and last_tick is not None:
                    quiet = (now - to_utc(last_tick)).total_seconds()
                    dark = quiet > self.config.ops_feed_stale_seconds
            except Exception:  # noqa: BLE001 - a provider that will not answer IS the symptom
                dark = open_now
            self._observe_ops(
                "symbol_feed_stale", active=dark, scope=symbol, detail=f"{symbol}"
            )

        try:
            pending = self.outbox.pending_count()
        except Exception:  # noqa: BLE001
            pending = 0
        self._observe_ops(
            "outbox_backlog",
            active=pending > self.config.ops_outbox_backlog,
            detail=f"{pending} undelivered",
        )

    def _state_of(self, symbol: str) -> MarketState:
        """One symbol's state from this poll's cache, filling it if nothing has yet (11B).

        The cache is filled by ``_before_poll``, which runs first in every live poll; the
        fallback covers ``startup`` and any caller that reaches a report directly. Going
        through here rather than calling the service is what keeps a market-state service
        that RAISES from taking the observer down: it used to be called uncaught from the
        state write, so a terminal that went away mid-candle propagated out of the poll.
        """
        if symbol not in self._states:
            self._market_states()
        return self._states.get(symbol, MarketState.UNKNOWN)

    def _market_is_open(self, symbol: str) -> bool:
        return self._state_of(symbol) is MarketState.OPEN

    def _observe_ops(self, name: str, **kwargs: object) -> None:
        try:
            self.ops.observe(name, **kwargs)  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 - reporting must never stop observation
            log.debug("could not record ops event %s", name, exc_info=True)

    def _check_alerts(self) -> None:
        """Answer every armed alert the current quotes have crossed (9C).

        On the poll clock rather than the candle clock: "the first quote across the level"
        is what a human asked about, and answering on candle close would tell them five
        minutes later about a move that may already have reversed.

        The snapshot is built here because this is the process that has the indicators --
        the same fields ``/status`` renders, so a reminder and the panel cannot describe one
        moment differently. Discord posts from the frozen copy (9C).
        """
        if self.alerts is None:
            return
        timeframe = self.config.timeframes[0]
        for symbol in self.config.symbols:
            armed = self.alerts.alerts.armed(symbol=symbol)
            if not armed:
                # The common case, and the cheap one: no quote is read for a symbol nobody
                # is watching a level on.
                continue
            quote = self._latest_quote(symbol)
            if quote is None:
                continue
            fired = self.alerts.check(
                symbol,
                quote,
                snapshot=build_snapshot(
                    quote=quote,
                    state_fields=self._snapshot(symbol, timeframe).as_state(),
                    **self._context_for_snapshot(symbol, timeframe),
                ),
            )
            for alert in fired:
                log.info(
                    "alert %s answered at %s (level %s)",
                    alert.alert_id,
                    alert.price,
                    alert.level,
                )

    def _context_for_snapshot(self, symbol: str, timeframe: Timeframe) -> dict[str, object]:
        """9B's profile summaries and volatility, for a fired alert's snapshot."""
        context = self._market_context(symbol, timeframe)
        return {
            "profiles": context.get("volume_profile") or None,
            "volatility": context.get("volatility"),
        }

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
                snapshot = self._snapshot(symbol, timeframe)
                snapshot_fields = snapshot.as_state()
                snapshot_fields["session_live_trend"] = self._live_session_trend(symbol, snapshot)
                symbols.append(
                    SymbolState(
                        symbol=symbol,
                        timeframe=timeframe,
                        market_state=self._state_of(symbol),
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
                        **snapshot_fields,
                        **self._market_context(symbol, timeframe),
                        trend_read=self._trend_read(symbol, timeframe),
                        mtf=self._mtf_read(symbol, timeframe),
                    )
                )
        try:
            self.state_repository.write(  # type: ignore[attr-defined]
                SystemState(
                    symbols=tuple(symbols),
                    account_mode=self._account_mode(),
                    # 11B: so Discord can say "closed until Sunday 22:00" without
                    # computing the weekly boundary itself.
                    sleep_phase=self.sleep.phase,
                    next_market_open=(
                        self.sleep.next_open(self._now_or_none())
                        if self.sleep.asleep
                        else None
                    ),
                ),
                force=force,
            )
        except Exception:  # noqa: BLE001 - state reporting must not stop observation
            log.exception("system_state write failed")

    def _live_session_trend(self, symbol: str, snapshot: MarketSnapshot) -> str | None:
        """Direction of the current in-progress session, computed by the observer.

        This deliberately uses the same per-symbol flat threshold as the completed
        SessionTrendAgent. Discord only renders the stored result; it does not derive a
        direction from prices on its own.
        """
        if snapshot.session_open is None or snapshot.session_close is None:
            return None
        tuning = tuning_for(symbol)
        change_points = (snapshot.session_close - snapshot.session_open) / tuning.point
        if abs(change_points) < tuning.flat_points:
            return "flat"
        return "up" if change_points > 0 else "down"

    def _account_mode(self) -> object:
        """Which account this terminal is logged into (11A, F-3).

        Read once and cached: it cannot change without a reconnect, and asking the terminal
        on every throttled state write would be a round trip per candle for a value that is
        fixed for the life of the connection.

        A provider that cannot answer leaves it unset rather than guessing DEMO. The field
        is for display and for evidence; nothing gates on it, because the executor reads its
        own broker rather than trusting a document another process wrote.
        """
        if self._cached_account_mode is not None:
            return self._cached_account_mode
        reader = getattr(self.provider, "account_info", None)
        if reader is None:
            return None
        try:
            from aureon.models.enums import AccountMode

            info = reader()
            raw = info.get("account_mode") if isinstance(info, dict) else None
            mode = (
                AccountMode(raw)
                if raw in {m.value for m in AccountMode}
                else AccountMode.from_trade_mode(
                    info.get("trade_mode") if isinstance(info, dict) else None
                )
            )
        except Exception:  # noqa: BLE001 - state reporting must not stop observation
            log.debug("could not read the account mode", exc_info=True)
            return None
        self._cached_account_mode = mode
        if mode.is_real_money:
            log.warning(
                "observing on a %s account — observation is read-only; the executor "
                "refuses execution unless AUREON_ALLOW_LIVE_EXECUTION=true",
                mode.value,
            )
        return mode

    def _trend_read(self, symbol: str, timeframe) -> object:
        """What the last N closed candles did, computed HERE and published (9D).

        The observer does it because the observer has the candles. Discord holds no data
        provider at all -- a boundary test enforces that by inspecting ``BotContext``'s own
        annotations -- so `/monitor` reads this field rather than deriving it, and the two
        processes cannot come to different conclusions about the same window (decision 194).

        The EMA endpoints are computed from the SAME candles and the SAME configured periods
        the agents use. A second EMA over a different window would eventually disagree with
        the one the detections were stamped with, and the disagreement would appear in a
        readout about those very detections.
        """
        from aureon.services.assessment_service import DEFAULT_TREND_CANDLES, read_trend

        tracker = self.engines.for_symbol(symbol).context_tracker(symbol, timeframe)
        if tracker is None:
            # No candle has closed yet. An absent read is honest; an empty one would render
            # as a market that was measured and found to be going nowhere.
            return None

        candles = tracker.recent(DEFAULT_TREND_CANDLES)
        if not candles:
            return None

        fields = self._snapshot(symbol, timeframe).as_state()
        context = self._market_context(symbol, timeframe)
        profiles = context.get("volume_profile") or {}
        tuning = tuning_for(symbol)

        try:
            return read_trend(
                candles,
                ema_fast=fields.get("ema_fast"),
                ema_slow=fields.get("ema_slow"),
                ema_fast_earlier=self._ema_fast_at_start(candles),
                session_trend=fields.get("session_trend"),
                asia_profile=profiles.get("asia"),
                volatility=context.get("volatility"),
                minutes_since_opposite_cross=minutes_since(
                    fields.get("last_cross_at"), self.provider.now_utc()
                ),
                point=tuning.point,
                flat_points=tuning.flat_points,
            )
        except Exception:  # noqa: BLE001 - a trend read must never stop observation
            log.exception("trend read failed for %s", symbol)
            return None

    def _ema_fast_at_start(self, candles) -> float | None:
        """The fast EMA at the START of the window, for the slope.

        Computed over the window with the configured fast period rather than stored,
        because nothing keeps an EMA series: the snapshot holds the latest value only. The
        first `fast_period` candles are warm-up, so a window shorter than that yields no
        slope rather than one measured from a half-warmed average.
        """
        from aureon.engine.indicators import ema

        period = self.config.ema_fast
        if len(candles) <= period:
            return None
        try:
            import pandas as pd

            series = ema(pd.Series([c.close for c in candles], dtype="float64"), period)
        except Exception:  # noqa: BLE001 - diagnostic, never critical
            log.debug("could not compute the window's EMA", exc_info=True)
            return None
        value = series.iloc[period]
        return None if pd.isna(value) else float(value)

    def _mtf_read(self, symbol: str, timeframe) -> object:
        """This stream's higher-timeframe reads, for ``system_state`` (11D).

        Read from the engine rather than recomputed, for the same reason the cross counters
        are: a second computation would eventually disagree with the one the detections carry,
        and then two documents written in the same second would describe different markets.
        """
        try:
            return self.engines.for_symbol(symbol).mtf_context(symbol, timeframe)
        except Exception:  # noqa: BLE001 - state reporting must not stop observation
            log.debug("could not read the mtf context for %s", symbol, exc_info=True)
            return None

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

        # Publish the current broker-day M5 history BEFORE processing new detections. This makes
        # a setup card rendered during startup immediately receive real candles rather than a
        # 0-bar placeholder.
        self._hydrate_live_chart_day(symbol, timeframe, candles)

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
        # The day in progress is deliberately NOT written here (11D). It is incomplete by
        # definition, and a document flagged complete=False is one mistake away from an
        # aggregation reading it as finished. The parquet archive below keeps the bars.
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
        # final=True: the thread is stopped, and a flush that honoured the stop flag would
        # deliver nothing at all -- which is what it did until 11B.
        remaining = self.worker.flush(final=True)
        if remaining:
            log.info("delivered %d detection(s) during shutdown", remaining)
        still = self.outbox.pending_count()
        if still:
            # Not an error: the queue is durable and the next boot drains it first.
            log.warning("%d detection(s) remain queued; they will be delivered next start", still)
        self.state.save()
        self.provider.close()
        flushed(
            paths.SERVICE_OBSERVER,
            queued=still,
            delivered_at_shutdown=remaining,
        )


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
    from aureon.storage.runtime import build_storage

    provider = MT5DataProvider(
        market_tz=config.market_tz,
        login=config.mt5_login,
        password=config.mt5_password,
        server=config.mt5_server,
        terminal_path=config.mt5_terminal_path,
    )
    storage = build_storage(
        account_scope=config.account_scope,
        state_heartbeat_seconds=config.state_heartbeat_seconds,
    )
    detections = storage.detections
    outbox = LocalOutbox(config.outbox_path)
    worker = OutboxWorker(outbox, detections.upsert_payload)
    heartbeat_repo = storage.heartbeats

    observer = Observer(
        config,
        provider,
        outbox=outbox,
        worker=worker,
        state=ObserverState(config.observer_state_path),
        market_state=MarketStateService(provider),
        state_repository=storage.system_state,
        session_repository=storage.sessions,
        symbol_repository=storage.symbols,
        evaluation_repository=storage.evaluations,
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
        # 9C: the observer answers the price alerts Discord armed, from the quotes it is
        # already reading.
        alert_repository=storage.alerts,
    )
    # 11A F-15. Assigned after construction rather than passed in, so an Observer built by a
    # test has no register and reports nothing -- which is what a test of observation wants.
    observer.ops = OpsRegister(storage.ops, service=paths.SERVICE_OBSERVER)
    # 11D, and assigned the same way for the same reason: a test of observation should not
    # have to stand up a day cache to watch a candle close.
    observer.market_days = storage.market_days
    _wire_setups(observer, config, storage)
    return observer


def _wire_setups(observer: Observer, config: AureonConfig, storage: object) -> None:
    """One setup engine per configured symbol (12, T-7).

    Per symbol rather than one shared engine, for the reason every other per-symbol thing in this
    process is: the engine caches the day's open setups, and one cache across two instruments
    would let silver's candles advance gold's setups. The point comes from the symbol's own
    tuning, because the id's anchor bin falls back to it when ATR is unknown.

    ``now`` is the CANDLE's close, not the wall clock. A replay of an archived day must stamp the
    same ``opened_at`` the live session did, or every replayed setup differs from its original in
    a field nobody meant to compare -- and the live-vs-replay check would report a difference
    that is only the clock.

    ## The reference book (T-9)

    Each engine is handed a callable that measures a new setup against the record of past ones.
    A callable rather than the book itself, so the engine depends on "something that returns a
    block" and not on Firestore, a rule or an evidence directory -- which is what keeps every
    lifecycle test in ``test_setup_engine`` constructible without any of the three.

    ``verified_market_dates`` is read ONCE per process here, not per setup. It reads a directory,
    and a filesystem walk inside the candle loop is the kind of cost that only shows up in
    production. The consequence is stated rather than hidden: a session verified while the
    observer is running is not counted until the next restart, which matters for one field on a
    research block and for nothing else.
    """
    from aureon.config.symbol_tuning import tuning_for
    from aureon.evaluation.rules import get_rule
    from aureon.services.session_evidence import verified_market_dates
    from aureon.services.setup_engine import SetupEngine
    from aureon.services.setup_evaluation import SetupEvaluator
    from aureon.services.setup_reference import ReferenceBook
    repository = storage.setups
    evaluations = storage.setup_evaluations
    for symbol in config.symbols:
        point = tuning_for(symbol).point
        rule = get_rule(config.rule_id_for(symbol))
        book = ReferenceBook(
            symbol=symbol,
            rule=rule,
            repository=evaluations,
            real_days=verified_market_dates(symbol),
            now=lambda: observer._setup_clock,
        )
        observer.setup_references[symbol] = book
        observer.setups[symbol] = SetupEngine(
            account_scope=config.account_scope,
            symbol=symbol,
            timeframe=config.timeframes[0],
            repository=repository,
            point=point,
            market_tz=config.market_tz,
            reference=book.for_setup,
            now=lambda: observer._setup_clock,
        )
        observer.setup_evaluators[symbol] = SetupEvaluator(
            rule=rule,
            market_tz=config.market_tz,
            point=point,
            repository=evaluations,
        )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    config = AureonConfig.from_env()
    observer = build_observer(config)

    install_handlers(observer.market_engine.stop, service=paths.SERVICE_OBSERVER)
    observer.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""The position monitor: MT5 is the truth (§49-§53, §58, §77).

Polls the broker and makes Firestore agree with it. Aureon never decides a trade closed;
it observes that it did -- which is why a close from the MT5 mobile app, a stop-loss hit
overnight, or a position opened by hand in the terminal all land correctly without Aureon
being involved in any of them.

## It must keep running when trading is disabled (§58)

``trading_enabled=false`` stops the *executor*. It must not stop the monitor: positions
opened before the switch was thrown are still live, still moving, and still need their
closes recorded. A monitor that paused with the executor would leave the operator blind
during exactly the incident that made them disable trading.

## The deal history overlap

Each poll re-reads deals from a little **before** the last sync, not from it. Broker deal
timestamps and our clock are not the same clock, and a deal can be published slightly out
of order -- reading from exactly the last sync point would drop such a deal permanently.
Re-reading is free because every write here is idempotent.

## External positions are imported, never managed (§52)

Anything without Aureon's magic number, or with it but no matching request, is recorded as
``source=external_mt5`` with ``trade_request_id=None``. Aureon reports on it and leaves it
alone: it did not open it and has no authorisation to touch it.

This is deliberately **not** filtered to the observed symbols. A position the trader opened
by hand is part of the account's exposure whether or not Aureon watches that instrument, and
a monitor that hid it would make `/status` read as flat while money was at risk. What such a
trade does not get is analysis it cannot support: no detections, no evaluations, and -- until
9A -- excursions divided by whatever tick the process was configured with.

## Excursions use each position's own tick (9A)

The tick comes from the broker's spec for that symbol, cached, rather than one ``point`` for
the process. Gold's 0.01 against silver's 0.001 is a tenfold error in a figure nobody can
sanity-check by eye, and the same applies to any instrument opened by hand. A symbol whose
spec cannot be read is left unmeasured rather than measured wrongly.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from aureon.execution.broker_interface import BrokerInterface
from aureon.models.base import MarketTime, to_utc, utc_now
from aureon.models.broker import BrokerDeal, BrokerPosition
from aureon.management.profit_guardian import ProfitGuardianAgent
from aureon.management.trade_manager import TradeManagementAgent
from aureon.models.enums import Direction, TradeRequestStatus, TradeSource, TradeStatus, TrendBias
from aureon.models.trade import Trade
from aureon.positions.deal_reconciler import PositionOutcome, summarise_position
from aureon.positions.excursion_tracker import ExcursionTracker
from aureon.positions.pending_order_monitor import PendingOrderMonitor
from aureon.storage.trade_repository import TradeRepository, trade_id_for
from aureon.storage.trade_request_repository import TradeRequestRepository

log = logging.getLogger(__name__)

DEFAULT_POLL_SECONDS = 2.0

# How far before the last sync to re-read deals. See the module docstring.
DEAL_OVERLAP_SECONDS = 300.0

# How far back to look on a cold start, when there is no last sync at all.
COLD_START_HOURS = 72.0

# Forward allowance on the deal window for broker/host clock skew.
CLOCK_SKEW_ALLOWANCE_SECONDS = 60.0


@dataclass
class PollResult:
    """What one poll changed."""

    opened: list[str] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    partially_closed: list[str] = field(default_factory=list)
    imported_external: list[str] = field(default_factory=list)
    pending_resolved: list[str] = field(default_factory=list)
    excursions_updated: int = 0

    @property
    def changed(self) -> bool:
        return bool(
            self.opened
            or self.closed
            or self.partially_closed
            or self.imported_external
            or self.pending_resolved
        )


class PositionMonitor:
    """Keeps ``trades`` in step with the broker (§49-§53)."""

    def __init__(
        self,
        trades: TradeRepository,
        requests: TradeRequestRepository,
        broker: BrokerInterface,
        *,
        magic: int,
        account_scope: str = "primary",
        market_tz: str = "Etc/UTC",
        point: float = 0.01,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        before_poll: object | None = None,
        parked: object | None = None,
        pace: object | None = None,
        market_context_provider: object | None = None,
        deal_overlap_seconds: float = DEAL_OVERLAP_SECONDS,
    ) -> None:
        self.trades = trades
        self.requests = requests
        self.broker = broker
        self.magic = magic
        self.account_scope = account_scope
        self.market_tz = market_tz
        self.point = point
        self.poll_seconds = poll_seconds
        #: Called first in every loop iteration (11B): the monitor's sleep decision.
        self.before_poll = before_poll
        #: Whether to skip this poll entirely (11B). Unlike the executor, the monitor IS
        #: parked: every number it records comes from a quote, and a closed market has no
        #: new quotes -- polling one would rewrite the same excursions with the same prices
        #: for forty-eight hours. The one full reconciliation at the close is what makes
        #: that safe, and it runs from the sleep hook rather than from here.
        self.parked = parked
        #: Given the awake cadence, the wait before the next iteration (11B).
        self.pace = pace
        self.market_context_provider = market_context_provider
        self.deal_overlap_seconds = deal_overlap_seconds

        # Agents 14 and 16 never call the broker themselves. The monitor supplies quotes
        # and persists their proposed management state; execution remains a separate boundary.
        self.trade_manager = TradeManagementAgent(primary_target_move=5.0, protect_after_move=5.0, protect_fraction=0.80)
        self.profit_guardian = ProfitGuardianAgent(primary_target_move=5.0, minimum_lock=4.0)

        #: symbol -> tick, from the broker's own spec. Cached: it does not change within a
        #: session, and one lookup per new symbol is cheaper than one per poll.
        self._points: dict[str, float] = {}
        self.excursions = ExcursionTracker(point=point, point_for=self.point_for)
        self.pending = PendingOrderMonitor(requests, broker, magic=magic)
        self._last_sync: datetime | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def point_for(self, symbol: str) -> float | None:
        """The broker's tick for one symbol (9A).

        The broker rather than the config, because the account can hold a position on an
        instrument Aureon does not observe -- a hand-opened trade is still exposure (§52) --
        and the terminal knows every symbol's tick while a tuning table only knows the ones
        somebody reviewed.
        """
        cached = self._points.get(symbol)
        if cached:
            return cached
        info = self.broker.symbol_info(symbol)
        point = getattr(info, "point", None)
        if not point or point <= 0:
            log.error("broker reported no usable tick for %s", symbol)
            return None
        self._points[symbol] = point
        return point

    # ── Startup (§77) ─────────────────────────────────────────────────────────

    def startup(self, *, now: datetime | None = None) -> PollResult:
        """Load stored state, query the broker, reconcile, then serve (§77).

        Reconciling before serving matters because the gap is the interesting part: a
        position may have closed, a pending order may have filled, and a human may have
        opened something new, all while this process was not running.
        """
        moment = to_utc(now or utc_now())
        open_trades = self.trades.open_trades()
        pending_requests = self.requests.list_by_status(
            [TradeRequestStatus.PENDING, TradeRequestStatus.PARTIALLY_FILLED]
        )
        log.info(
            "monitor starting: %d open trade(s), %d pending request(s) to verify",
            len(open_trades),
            len(pending_requests),
        )

        # Resume excursion tracking, and rebuild it from candles for any position that
        # was open while we were down (§45).
        for trade in open_trades:
            self.excursions.track(trade)

        return self.poll_once(now=moment)

    # ── One poll ──────────────────────────────────────────────────────────────

    def poll_once(self, *, now: datetime | None = None) -> PollResult:
        """Read the broker and reconcile Firestore to it."""
        moment = to_utc(now or utc_now())
        result = PollResult()

        positions = {p.position_id: p for p in self.broker.open_positions()}
        resting = {o.order_ticket for o in self.broker.pending_orders()}
        deals = self._recent_deals(moment)

        self._resolve_pending(deals, resting, result, now=moment)
        self._import_open(positions, result, now=moment)
        self._close_vanished(positions, deals, result, now=moment)
        self._apply_partial_closes(positions, deals, result, now=moment)
        self._update_excursions(positions, result)

        self._last_sync = moment
        return result

    def _recent_deals(self, now: datetime) -> list[BrokerDeal]:
        start = (
            self._last_sync - timedelta(seconds=self.deal_overlap_seconds)
            if self._last_sync is not None
            else now - timedelta(hours=COLD_START_HOURS)
        )
        # The END bound reaches slightly into the future: the broker's clock is not ours,
        # and a deal stamped a second ahead of us would otherwise be invisible for a whole
        # poll -- long enough for the close path to see only part of a multi-deal exit.
        end = now + timedelta(seconds=CLOCK_SKEW_ALLOWANCE_SECONDS)
        try:
            return self.broker.deals_history(start, end)
        except Exception:  # noqa: BLE001 - a read failure must not stop the loop
            log.exception("deals_history failed")
            return []

    # ── Pending orders ────────────────────────────────────────────────────────

    def _resolve_pending(
        self,
        deals: list[BrokerDeal],
        resting: set[int],
        result: PollResult,
        *,
        now: datetime,
    ) -> None:
        for request in self.requests.list_by_status(TradeRequestStatus.PENDING):
            outcome = self.pending.check(
                request, deals=deals, resting_tickets=resting, now=now
            )
            if outcome is None:
                continue
            result.pending_resolved.append(request.request_id)
            if outcome.created_a_position and outcome.position_id is not None:
                # It filled while we were away; the position needs a Trade.
                self._record_open_position(
                    outcome.position_id, deals, request_id=request.request_id, result=result
                )

    # ── Opening ───────────────────────────────────────────────────────────────

    def _import_open(
        self, positions: dict[int, BrokerPosition], result: PollResult, *, now: datetime
    ) -> None:
        """Ensure every open broker position has a Trade document (§52)."""
        for position_id, position in positions.items():
            trade_id = trade_id_for(position_id, account_scope=self.account_scope)
            if self.trades.get(trade_id) is not None:
                continue

            request_id = self._request_for(position)
            external = position.magic != self.magic or request_id is None
            trade = Trade(
                trade_id=trade_id,
                mt5_position_id=position_id,
                trade_request_id=request_id,
                source=TradeSource.EXTERNAL_MT5 if external else TradeSource.AUREON,
                symbol=position.symbol,
                direction=position.direction,
                volume=position.volume,
                open_price=position.open_price,
                open_time=MarketTime.from_utc(position.open_time, self.market_tz),
                sl=position.sl,
                tp=position.tp,
                magic=position.magic,
                status=TradeStatus.OPEN,
            )
            self.trades.upsert_open(trade)
            self.excursions.track(trade)
            (result.imported_external if external else result.opened).append(trade_id)
            log.info(
                "imported %s position %s (%s %s %.2f)",
                "external" if external else "aureon",
                position_id,
                position.symbol,
                position.direction.value,
                position.volume,
            )
            _ = now

    def _record_open_position(
        self,
        position_id: int,
        deals: list[BrokerDeal],
        *,
        request_id: str | None,
        result: PollResult,
    ) -> None:
        """Create a Trade for a position known only from its deals.

        Used when a pending order filled while the monitor was down: the position may
        already be closed again, so the broker's open-positions list will not show it.
        """
        trade_id = trade_id_for(position_id, account_scope=self.account_scope)
        if self.trades.get(trade_id) is not None:
            return
        outcome = summarise_position(deals, position_id)
        if not outcome.entry_deals:
            return
        entry = outcome.entry_deals[0]
        trade = Trade(
            trade_id=trade_id,
            mt5_position_id=position_id,
            trade_request_id=request_id,
            source=TradeSource.AUREON if request_id else TradeSource.EXTERNAL_MT5,
            symbol=entry.symbol,
            direction=entry.direction,
            volume=outcome.opened_volume,
            open_price=outcome.open_price or entry.price,
            open_time=MarketTime.from_utc(entry.executed_at, self.market_tz),
            magic=entry.magic,
            status=TradeStatus.OPEN,
        )
        self.trades.upsert_open(trade)
        result.opened.append(trade_id)

    def _request_for(self, position: BrokerPosition) -> str | None:
        """Find the request that opened this position, if Aureon opened it (§52).

        Matched on the comment token carried by the *entry*, which is the one place a
        comment is reliable. No match means external, and external means observed only.
        """
        if position.magic != self.magic or not position.comment:
            return None
        from aureon.models.identity import comment_token

        for status in (
            TradeRequestStatus.FILLED,
            TradeRequestStatus.PARTIALLY_FILLED,
            TradeRequestStatus.PENDING,
            TradeRequestStatus.EXECUTING,
        ):
            for request in self.requests.list_by_status(status, limit=200):
                if request.position_id == position.position_id:
                    return request.request_id
                if (request.comment_token or comment_token(request.request_id)) == position.comment:
                    return request.request_id
        return None

    # ── Closing ───────────────────────────────────────────────────────────────

    def _close_vanished(
        self,
        positions: dict[int, BrokerPosition],
        deals: list[BrokerDeal],
        result: PollResult,
        *,
        now: datetime,
    ) -> None:
        """A tracked position the broker no longer reports has closed (§44).

        The deals say how: stop-loss, take-profit, a phone, or a hand on the terminal.
        """
        for trade in self.trades.open_trades():
            if trade.mt5_position_id in positions:
                continue
            outcome = summarise_position(deals, trade.mt5_position_id)
            if not outcome.exit_deals:
                # Gone from the open list but no exit deal visible yet. Left alone rather
                # than guessed at: the deal usually appears within a poll or two, and
                # inventing a close price would be worse than waiting.
                log.info(
                    "position %s is gone but has no exit deal yet; waiting",
                    trade.mt5_position_id,
                )
                continue

            if outcome.closed_volume + 1e-9 < trade.volume:
                # The position is gone, so it IS fully closed -- but the deals we can see
                # do not account for all of it yet. CLOSED is terminal, so writing it now
                # would freeze a partial realized P&L that can never be corrected. Record
                # the progress instead and finish on a later poll, once the remaining
                # deals are visible.
                log.info(
                    "position %s is gone but deals cover only %s of %s; recording partial "
                    "and waiting for the rest",
                    trade.mt5_position_id,
                    outcome.closed_volume,
                    trade.volume,
                )
                self._record_partial(trade, outcome, result)
                continue

            self._apply_close(trade, outcome, result, now=now)

    def _apply_close(
        self, trade: Trade, outcome: PositionOutcome, result: PollResult, *, now: datetime
    ) -> None:
        final = self.excursions.release(trade.trade_id) or trade.excursion
        closed_volume = min(outcome.closed_volume, trade.volume)
        updates = {
            "closed_volume": round(closed_volume, 8),
            "close_price": outcome.close_price,
            "close_time": MarketTime.from_utc(
                outcome.exit_deals[-1].executed_at, self.market_tz
            ),
            "close_reason": outcome.close_reason,
            "close_reason_raw": outcome.close_reason_raw,
            "realized_pnl": outcome.realized_pnl,
            "commission": outcome.commission,
            "swap": outcome.swap,
            "deal_ids": outcome.deal_ids,
            "excursion": final,
        }
        self.trades.transition(
            trade.trade_id,
            TradeStatus.CLOSED,
            updates=updates,
            reason=f"closed by {outcome.close_reason}",
        )
        result.closed.append(trade.trade_id)
        log.info(
            "trade %s CLOSED at %s (%s), P&L %.2f",
            trade.trade_id,
            outcome.close_price,
            outcome.close_reason,
            outcome.realized_pnl,
        )
        _ = now

    def _record_partial(
        self, trade: Trade, outcome: PositionOutcome, result: PollResult
    ) -> None:
        """Record partial-close progress without claiming the trade is finished."""
        closed = min(round(outcome.closed_volume, 8), trade.volume)
        if closed <= trade.closed_volume + 1e-9:
            return
        self.trades.transition(
            trade.trade_id,
            TradeStatus.PARTIALLY_CLOSED,
            updates={
                "closed_volume": closed,
                "realized_pnl": outcome.realized_pnl,
                "commission": outcome.commission,
                "swap": outcome.swap,
                "deal_ids": outcome.deal_ids,
                "close_reason": outcome.close_reason,
                "close_reason_raw": outcome.close_reason_raw,
            },
            reason=f"partial close of {closed} by {outcome.close_reason}",
        )
        if trade.trade_id not in result.partially_closed:
            result.partially_closed.append(trade.trade_id)

    def _apply_partial_closes(
        self,
        positions: dict[int, BrokerPosition],
        deals: list[BrokerDeal],
        result: PollResult,
        *,
        now: datetime,
    ) -> None:
        """A position still open but smaller than we recorded was partly closed (§44)."""
        for trade in self.trades.open_trades():
            position = positions.get(trade.mt5_position_id)
            if position is None:
                continue
            outcome = summarise_position(deals, trade.mt5_position_id)
            if not outcome.exit_deals:
                continue
            closed = min(round(outcome.closed_volume, 8), trade.volume)
            if closed <= trade.closed_volume + 1e-9:
                continue  # nothing new
            self.trades.transition(
                trade.trade_id,
                TradeStatus.PARTIALLY_CLOSED,
                updates={
                    "closed_volume": closed,
                    "realized_pnl": outcome.realized_pnl,
                    "commission": outcome.commission,
                    "swap": outcome.swap,
                    "deal_ids": outcome.deal_ids,
                    "close_reason": outcome.close_reason,
                    "close_reason_raw": outcome.close_reason_raw,
                },
                reason=f"partial close of {closed} by {outcome.close_reason}",
            )
            result.partially_closed.append(trade.trade_id)
            log.info(
                "trade %s partially closed: %s of %s remains open",
                trade.trade_id,
                position.volume,
                trade.volume,
            )
            _ = now

    # ── Excursions ────────────────────────────────────────────────────────────

    def _update_excursions(
        self, positions: dict[int, BrokerPosition], result: PollResult
    ) -> None:
        """Fold the current quote into each open position's excursions (§45)."""
        symbols = {p.symbol for p in positions.values()}
        quotes = {}
        for symbol in symbols:
            try:
                quotes[symbol] = self.broker.quote(symbol)
            except Exception:  # noqa: BLE001 - excursions are diagnostic, never critical
                log.exception("quote failed for %s", symbol)

        for position in positions.values():
            quote = quotes.get(position.symbol)
            if quote is None:
                continue
            trade_id = trade_id_for(position.position_id, account_scope=self.account_scope)
            updated = self.excursions.on_quote(trade_id, quote)
            if updated is not None:
                self.trades.update_excursion(trade_id, updated)
                result.excursions_updated += 1
            self._update_management_state(trade_id, position, quote)

    def _update_management_state(self, trade_id: str, position, quote) -> None:
        """Run Agents 14/16 for an Aureon-owned position from broker truth + observer context."""

        try:
            trade = self.trades.get(trade_id)
        except Exception:  # noqa: BLE001
            log.exception("could not load %s for management", trade_id)
            return
        if trade is None or trade.source is not TradeSource.AUREON:
            return

        current_price = quote.bid if trade.direction is Direction.BUY else quote.ask
        excursion = self.excursions.current(trade_id) or trade.excursion
        peak_price = excursion.mfe_price if excursion.mfe_price is not None else current_price

        management = self.trade_manager.assess(
            direction=trade.direction,
            entry_price=trade.open_price,
            current_price=current_price,
            peak_price=peak_price,
        )

        guardian = None
        if management.target_reached:
            guardian = self.profit_guardian.assess(
                direction=trade.direction,
                entry_price=trade.open_price,
                current_price=current_price,
                peak_price=peak_price,
                market_health=self._market_health(trade.symbol, trade.direction),
            )

        try:
            self.trades.update_management(
                trade_id,
                management=management,
                guardian=guardian,
            )
        except Exception:  # noqa: BLE001 - management context must never stop reconciliation
            log.exception("could not persist management state for %s", trade_id)

    def _market_health(self, symbol: str, direction: Direction) -> dict[str, bool | None]:
        """Translate observer state into the Guardian's named health checks.

        Missing observer state stays None. It is not counted as healthy or unhealthy.
        """

        if self.market_context_provider is None:
            return {}
        try:
            state = self.market_context_provider(symbol)  # type: ignore[operator]
        except Exception:  # noqa: BLE001
            log.debug("could not read market context for %s", symbol, exc_info=True)
            return {}
        if state is None:
            return {}

        ema = None
        if getattr(state, "ema_fast", None) is not None and getattr(state, "ema_slow", None) is not None:
            ema = (
                state.ema_fast > state.ema_slow
                if direction is Direction.BUY
                else state.ema_fast < state.ema_slow
            )

        session = getattr(state, "session_live_trend", None)
        session_ok = None
        if session in {"up", "down"}:
            session_ok = (
                session == "up" if direction is Direction.BUY else session == "down"
            )

        htf = getattr(state, "higher_timeframe_agent", None)
        htf_ok = None
        if htf is not None and htf.dominant_bias is not TrendBias.SIDEWAYS:
            htf_ok = (
                htf.dominant_bias is TrendBias.BULLISH
                if direction is Direction.BUY
                else htf.dominant_bias is TrendBias.BEARISH
            )

        regime = getattr(state, "market_regime", None) or {}
        regime_name = regime.get("regime") if isinstance(regime, dict) else None
        regime_ok = None if regime_name is None else regime_name not in {
            "volatile_chop",
            "structurally_messy",
        }

        participation = getattr(state, "volume_participation", None) or {}
        impulse = participation.get("price_impulse") if isinstance(participation, dict) else None
        participation_ok = None
        if impulse in {"bullish", "bearish"}:
            participation_ok = (
                impulse == "bullish"
                if direction is Direction.BUY
                else impulse == "bearish"
            )

        return {
            "ema": ema,
            "session": session_ok,
            "htf": htf_ok,
            "regime": regime_ok,
            "participation": participation_ok,
        }

    def reconstruct_excursions(
        self, trade: Trade, candles: list, *, until: datetime | None = None
    ) -> None:
        """Rebuild a position's excursions from M1 candles and store them (§45)."""
        from aureon.positions.excursion_tracker import reconstruct_from_candles

        # This symbol's tick, not the process's (9A). A reconstruction is already the
        # weaker measurement; dividing it by the wrong tick would make it a wrong one.
        point = self.point_for(trade.symbol)
        if point is None:
            log.error(
                "no tick for %s; not reconstructing excursions for %s",
                trade.symbol,
                trade.trade_id,
            )
            return
        rebuilt = reconstruct_from_candles(trade, candles, point=point, until=until)
        self.trades.update_excursion(trade.trade_id, rebuilt)
        # Re-track so live ticks continue from the reconstructed extremes, keeping the
        # weaker `reconstructed` label.
        self.excursions.release(trade.trade_id)
        self.excursions.track(trade.model_copy(update={"excursion": rebuilt}))

    # ── Loop ──────────────────────────────────────────────────────────────────

    def run(self) -> None:
        """Poll until stopped.

        Deliberately independent of ``trading_enabled`` (§58): disabling trading stops the
        executor, not the recording of positions that are already live.
        """
        while not self._stop.is_set():
            if self.before_poll is not None:
                try:
                    self.before_poll()  # type: ignore[operator]
                except Exception:  # noqa: BLE001 - a side errand must not stop monitoring
                    log.exception("monitor before_poll failed")
            if not self.is_parked:
                try:
                    self.poll_once()
                except Exception:  # noqa: BLE001 - a poll failure must not kill the monitor
                    log.exception("monitor poll failed")
            self._stop.wait(self._wait())

    @property
    def is_parked(self) -> bool:
        """Fails toward polling: a hook that raises leaves the monitor watching (11B)."""
        if self.parked is None:
            return False
        try:
            return bool(self.parked())  # type: ignore[operator]
        except Exception:  # noqa: BLE001
            log.exception("monitor parked hook failed; staying awake")
            return False

    def _wait(self) -> float:
        if self.pace is None:
            return self.poll_seconds
        try:
            return float(self.pace(self.poll_seconds))  # type: ignore[operator]
        except Exception:  # noqa: BLE001
            log.exception("monitor pace hook failed; using the awake cadence")
            return self.poll_seconds

    def start(self) -> None:
        self._thread = threading.Thread(target=self.run, name="position-monitor", daemon=True)
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

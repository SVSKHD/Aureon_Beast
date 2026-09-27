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

import hashlib
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from aureon.execution.broker_interface import BrokerInterface
from aureon.management.exit_manager import DeterministicExitManager, ExitDecision, ExitPolicy
from aureon.models.base import MarketTime, to_utc, utc_now
from aureon.models.broker import BrokerDeal, BrokerPosition
from aureon.models.control import AUTONOMOUS_REQUESTER_PREFIX, ControlRequest
from aureon.models.enums import (
    ControlRequestKind,
    Direction,
    ManagementPhase,
    TradeRequestStatus,
    TradeSource,
    TradeStatus,
    TrendBias,
)
from aureon.models.trade import Trade, TradeManagementEvent, TradeManagementState
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
        exit_policy: ExitPolicy | None = None,
        controls: object | None = None,
        autonomous_management: bool = False,
        structural_invalidation_provider: object | None = None,
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

        # The deterministic exit manager wraps Agents 14 and 16. It never calls the broker:
        # the monitor persists its state and, only when autonomous management is enabled,
        # asks the EXECUTOR to move a stop or close through a control request under the
        # same claim/lease discipline a human's /close uses.
        self.exit_manager = DeterministicExitManager(exit_policy)
        self.trade_manager = self.exit_manager.trade_manager
        self.profit_guardian = self.exit_manager.guardian
        #: ``ControlRequestRepository`` (or None). Without one, decisions are recorded only.
        self.controls = controls
        #: Off by default: recording decisions is always safe; acting on them is opt-in.
        self.autonomous_management = autonomous_management
        #: ``callable(trade) -> str | None`` naming a structural invalidation, if any.
        self.structural_invalidation_provider = structural_invalidation_provider

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
            if not external:
                trade = trade.model_copy(
                    update={"management_state": self._initial_management(trade, now)}
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
        if trade.aureon_managed:
            trade = trade.model_copy(
                update={"management_state": self._initial_management(trade, entry.executed_at)}
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
        exit_time = outcome.exit_deals[-1].executed_at
        management_state = None
        if trade.aureon_managed:
            # The exit experience is derived once, from broker truth, and frozen with the
            # closed trade: exit price/time/reason, realised move, profit given back.
            base = trade.management_state or self._initial_management(trade, trade.open_time.utc)
            management_state = self.exit_manager.close(
                base,
                exit_price=outcome.close_price,
                exit_time=exit_time,
                exit_reason=outcome.close_reason,
            )
        updates = {
            "management_state": management_state,
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
        if management_state is not None:
            self._record_event(
                trade.trade_id,
                action="closed",
                state=management_state,
                previous_phase=(trade.management_state.phase if trade.management_state else None),
                observed_at=exit_time,
                exit_price=outcome.close_price,
                detail=f"broker close reason {outcome.close_reason}",
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

    def _initial_management(self, trade: Trade, at: datetime | None) -> TradeManagementState:
        return self.exit_manager.open(
            entry_price=trade.open_price,
            direction=trade.direction,
            initial_stop=trade.sl,
            now=at,
        )

    def _update_management_state(self, trade_id: str, position, quote) -> None:
        """Advance the deterministic exit manager for an AUREON-OWNED position (§52).

        An external/manual position is observed and displayed but never managed: it
        returns before any state is touched. The state persisted on the trade is the
        restart record -- a monitor that comes back up continues from the ratcheted stop
        it left, never from scratch.
        """

        try:
            trade = self.trades.get(trade_id)
        except Exception:  # noqa: BLE001
            log.exception("could not load %s for management", trade_id)
            return
        if trade is None or not trade.aureon_managed:
            return
        if trade.status is TradeStatus.CLOSED:
            return

        current_price = quote.bid if trade.direction is Direction.BUY else quote.ask
        state = trade.management_state or self._initial_management(trade, trade.open_time.utc)
        if state.is_terminal:
            return
        structural = None
        if self.structural_invalidation_provider is not None:
            try:
                structural = self.structural_invalidation_provider(trade)  # type: ignore[operator]
            except Exception:  # noqa: BLE001 - a broken provider is "no invalidation known"
                log.debug("structural invalidation provider failed", exc_info=True)
        try:
            advanced, decision = self.exit_manager.assess(
                state,
                current_price=current_price,
                market_health=self._market_health(trade.symbol, trade.direction),
                structural_invalidation=structural,
                now=quote.captured_at if getattr(quote, "captured_at", None) else None,
            )
        except Exception:  # noqa: BLE001 - management must never stop reconciliation
            log.exception("exit manager failed for %s", trade_id)
            return

        request_id = None
        if decision.actionable and self.autonomous_management and self.controls is not None:
            request_id = self._request_management_action(trade, advanced, decision)
            if request_id and decision.exit_requested:
                advanced = advanced.model_copy(update={"exit_request_id": request_id})

        try:
            self.trades.update_management(
                trade_id,
                management=decision.management,
                guardian=decision.guardian,
                management_state=advanced,
            )
        except Exception:  # noqa: BLE001 - management context must never stop reconciliation
            log.exception("could not persist management state for %s", trade_id)
            return
        if decision.phase_changed or decision.stop_changed or decision.exit_requested:
            self._record_event(
                trade_id,
                action=decision.action.value,
                state=advanced,
                previous_phase=decision.previous_phase,
                observed_at=advanced.updated_at or utc_now(),
                priority=decision.priority.value,
                previous_stop=decision.previous_stop,
                detail=decision.reason + (f"; control {request_id}" if request_id else ""),
            )

    def _request_management_action(
        self, trade: Trade, state: TradeManagementState, decision: ExitDecision
    ) -> str | None:
        """Hand a decision to the executor as a control request. Idempotent by content.

        The id is a hash of trade, kind and stop level, so the same decision reached again
        after a restart maps to the same request and the repository's create() returns the
        existing row instead of a second one. An exit already requested is never repeated.
        """
        if state.exit_request_id and decision.exit_requested:
            return state.exit_request_id
        if decision.exit_requested:
            kind = ControlRequestKind.CLOSE
            stop = None
        elif decision.stop_changed and decision.new_stop is not None:
            kind = ControlRequestKind.MODIFY_STOP
            stop = round(decision.new_stop, 5)
        else:
            return None
        digest = hashlib.sha256(
            f"{trade.trade_id}|{kind.value}|{stop}".encode()
        ).hexdigest()[:16]
        request = ControlRequest(
            control_id=f"mgmt-{digest}",
            kind=kind,
            target=str(trade.mt5_position_id),
            symbol=trade.symbol,
            stop_loss=stop,
            requested_by=f"{AUTONOMOUS_REQUESTER_PREFIX}exit_manager",
        )
        try:
            created = self.controls.create(request)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - a lost request is retried on the next quote
            log.exception("could not write management control request for %s", trade.trade_id)
            return None
        return created.control_id

    def _record_event(
        self,
        trade_id: str,
        *,
        action: str,
        state: TradeManagementState,
        previous_phase: ManagementPhase | None,
        observed_at: datetime,
        priority: str | None = None,
        previous_stop: float | None = None,
        exit_price: float | None = None,
        detail: str | None = None,
    ) -> None:
        append = getattr(self.trades, "append_management_event", None)
        if append is None:
            return
        moment = to_utc(observed_at)
        digest = hashlib.sha256(
            f"{trade_id}|{action}|{state.phase.value}|{state.current_stop}|{moment.isoformat()}".encode()
        ).hexdigest()[:24]
        event = TradeManagementEvent(
            event_id=f"mgmt_{digest}",
            trade_id=trade_id,
            observed_at=moment,
            source="exit_manager",
            action=action,
            current_move=state.current_move,
            peak_move=state.mfe,
            giveback=max(0.0, state.mfe - state.current_move),
            protected_move=(
                None
                if state.current_stop is None
                else (state.current_stop - state.entry_price) * state.direction.sign
            ),
            trail_price=state.current_stop,
            exit_price=exit_price if exit_price is not None else state.exit_price,
            realized_move=state.realized_move,
            exit_reason=state.exit_reason,
            phase=state.phase.value,
            previous_phase=None if previous_phase is None else previous_phase.value,
            priority=priority or state.exit_priority.value,
            current_stop=state.current_stop,
            previous_stop=previous_stop,
            detail=detail,
        )
        try:
            append(event)
        except Exception:  # noqa: BLE001 - the audit row must never stop reconciliation
            log.exception("could not append management event for %s", trade_id)

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
        ema_fast = getattr(state, "ema_fast", None)
        ema_slow = getattr(state, "ema_slow", None)
        if ema_fast is not None and ema_slow is not None:
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

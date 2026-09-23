"""The executor: the only thing in Aureon that moves money (§29-§34).

One request at a time, in a fixed order: **claim → guard → build → send → resolve.**

## The rule the whole phase exists for

If a send raises, or times out, or the process dies mid-call, the request is **left in
``EXECUTING``**. Not FAILED. Not retried.

That feels wrong the first time -- a failed call obviously failed, surely? -- but the
broker may have accepted the order before the connection dropped. ``FakeBroker``'s
``crash_after_send`` is exactly that case, and from the caller's side it is
indistinguishable from ``crash_before_result`` where nothing happened. Marking it FAILED
invites a re-send; re-sending turns one authorisation into two real trades. So the
request stays EXECUTING, and ``ReconciliationService`` goes and *looks* at the broker.

A rejection is different. The broker answered, the answer was no, nothing exists: that
resolves straight to FAILED with the code.

## Waking up

The local runtime polls durable ``trade_requests`` for CONFIRMED requests. There is no
Firestore snapshot listener in the SQLite path; polling is the single wake-up mechanism.
"""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime

from aureon.execution.broker_interface import BrokerError, BrokerInterface
from aureon.execution.execution_guard import BrokerSnapshot, GuardResult, check
from aureon.execution.idempotency import AlreadySent, SendGuard, comment_token
from aureon.execution.lease_manager import LeaseManager
from aureon.models.base import utc_now
from aureon.models.enums import (
    AccountMode,
    FailureCode,
    MarketState,
    TradeRequestStatus,
)
from aureon.models.settings import ExecutionSettings
from aureon.models.trade import BrokerOrderRequest, BrokerOrderResult, TradeRequest
from aureon.storage.trade_request_repository import (
    ClaimRejected,
    LeaseLost,
    TradeRequestRepository,
)

log = logging.getLogger(__name__)

DEFAULT_POLL_SECONDS = 2.0


class ExecutionWorker:
    """Executes CONFIRMED trade requests, exactly once each."""

    def __init__(
        self,
        repository: TradeRequestRepository,
        broker: BrokerInterface,
        *,
        magic: int,
        settings_provider: object,
        market_state_provider: object | None = None,
        executor_id: str | None = None,
        lease_seconds: float = 60.0,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        allow_live_execution: bool = False,
        on_ops_event: object | None = None,
        before_poll: object | None = None,
        pace: object | None = None,
    ) -> None:
        self.repository = repository
        self.broker = broker
        self.magic = magic
        # Settings come from Firestore at execution time (decision 11), so
        # `/trading disable` takes effect on the very next request.
        self.settings_provider = settings_provider
        self.market_state_provider = market_state_provider
        self.executor_id = executor_id or f"exec-{uuid.uuid4().hex[:10]}"
        self.lease_seconds = lease_seconds
        self.poll_seconds = poll_seconds
        #: 11A F-3. From the environment, never from Firestore, and defaulting to False so a
        #: caller that forgets it refuses a live account rather than permitting one.
        self.allow_live_execution = allow_live_execution
        #: Optional ``callable(name, detail)`` for the ops channel (11A F-15).
        self.on_ops_event = on_ops_event
        #: Called first in every loop iteration (11B). The executor's sleep decision lives
        #: here, and note what is NOT here: there is no ``parked``. A closed market slows
        #: this loop down; it does not stop it. A human who confirms a trade on a Saturday
        #: is owed a refusal that says "market closed", not silence until Sunday night --
        #: and the refusal is the guard's, on this process's own broker, already.
        self.before_poll = before_poll
        #: Given the awake cadence, the wait before the next iteration (11B). Safe to slow
        #: right down because the snapshot listener, not the poll, is what makes a CONFIRMED
        #: request prompt; the poll is the fallback for a listener that could not start.
        self.pace = pace
        #: Set by ``announce_account_mode`` when the terminal turns out to be real money
        #: and nobody said that was intended. Reconcile-only: startup reconciliation runs
        #: as usual, and every request is refused before the broker is touched.
        self.reconcile_only = False
        self.account_mode = AccountMode.UNKNOWN

        self.send_guard = SendGuard()
        self.leases = LeaseManager(repository, self.executor_id, lease_seconds=lease_seconds)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.executed = 0
        self.refused = 0
        self.left_executing = 0

    # ── One request ───────────────────────────────────────────────────────────

    def process(self, request_id: str, *, now: datetime | None = None) -> TradeRequest | None:
        """Take a request from CONFIRMED to a resolved status.

        Returns ``None`` when another executor claimed it first, which is the normal
        outcome of a race and not an error.
        """
        moment = now or utc_now()

        try:
            claimed = self.repository.claim(
                request_id, self.executor_id, lease_seconds=self.lease_seconds, now=moment
            )
        except ClaimRejected as exc:
            log.info("not ours: %s", exc)
            return None

        settings = self._settings()
        verdict = self._guard(claimed, settings, now=moment)
        if not verdict.ok:
            self.refused += 1
            log.info(
                "guard refused %s at rule %s: %s", request_id, verdict.rule, verdict.message
            )
            return self._resolve(
                request_id,
                TradeRequestStatus.FAILED,
                failure_code=verdict.failure_code,
                failure_message=verdict.message,
            )

        return self._send_and_resolve(claimed, verdict, now=moment)

    def _send_and_resolve(
        self, request: TradeRequest, verdict: GuardResult, *, now: datetime
    ) -> TradeRequest | None:
        request_id = request.request_id
        token = comment_token(request_id)

        try:
            attempt = self.send_guard.begin(request_id)
        except AlreadySent as exc:
            # This instance already sent one. Leave it EXECUTING for reconciliation:
            # re-sending is the one thing that must never happen.
            log.error("%s", exc)
            self.left_executing += 1
            return None

        order = BrokerOrderRequest(
            symbol=request.symbol,
            order_type=request.order_type,
            volume=request.volume,
            price=request.price,
            sl=request.sl,
            tp=request.tp,
            deviation_points=verdict.effective_deviation_points,
            filling_mode=request.filling_mode,
            magic=self.magic,
            comment=token,
        )

        # Record the attempt's identity BEFORE sending, so a crash leaves the request
        # carrying the token reconciliation will search for (§34).
        try:
            self.repository.resolve(
                request_id,
                self.executor_id,
                TradeRequestStatus.EXECUTING,
                updates={
                    "comment_token": token,
                    "execution_attempt_id": attempt,
                    "magic": self.magic,
                    "deviation_points": verdict.effective_deviation_points,
                },
            )
        except LeaseLost as exc:
            log.warning("lease lost before sending %s: %s", request_id, exc)
            return None

        stop_keepalive = threading.Event()
        self.leases.keepalive(request_id, stop_keepalive)
        try:
            result = self._send(order)
        except BrokerError as exc:
            # THE critical path. The order may or may not exist. Status stays EXECUTING.
            self.left_executing += 1
            log.error(
                "unknown outcome for %s (attempt %s, token %s): %s -- left EXECUTING "
                "for reconciliation, NOT retried",
                request_id,
                attempt,
                token,
                exc,
            )
            return None
        except Exception:  # noqa: BLE001 - an unexpected error is still an unknown outcome
            self.left_executing += 1
            log.exception(
                "unexpected error sending %s (attempt %s, token %s) -- left EXECUTING",
                request_id,
                attempt,
                token,
            )
            return None
        finally:
            stop_keepalive.set()

        if self.leases.lost(request_id):
            # Another worker may own this now; do not write an outcome.
            log.warning("lease lost during send of %s; standing down", request_id)
            self.left_executing += 1
            return None

        return self._record(request, result, now=now)

    def _send(self, order: BrokerOrderRequest) -> BrokerOrderResult:
        if order.order_type.is_pending:
            return self.broker.send_pending_order(order)
        return self.broker.send_market_order(order)

    def _record(
        self, request: TradeRequest, result: BrokerOrderResult, *, now: datetime
    ) -> TradeRequest | None:
        request_id = request.request_id

        if not result.ok:
            # The broker answered "no". Nothing exists, so this is a clean failure.
            return self._resolve(
                request_id,
                TradeRequestStatus.FAILED,
                failure_code=result.failure_code or FailureCode.BROKER_REJECTED,
                failure_message=result.message or f"broker retcode {result.retcode}",
                updates={"order_ticket": result.order_ticket},
            )

        self.executed += 1
        updates = {
            "order_ticket": result.order_ticket,
            "position_id": result.position_id,
            "deal_ids": result.deal_ids,
            "fill_price": result.fill_price,
            "filled_volume": result.filled_volume,
        }

        if request.order_type.is_pending:
            status = TradeRequestStatus.PENDING
        elif (
            result.filled_volume is not None
            and result.filled_volume + 1e-9 < request.volume
        ):
            status = TradeRequestStatus.PARTIALLY_FILLED
        else:
            status = TradeRequestStatus.FILLED

        return self._resolve(request_id, status, updates=updates)

    def _resolve(
        self,
        request_id: str,
        status: TradeRequestStatus,
        *,
        updates: dict | None = None,
        failure_code: FailureCode | None = None,
        failure_message: str | None = None,
    ) -> TradeRequest | None:
        try:
            return self.repository.resolve(
                request_id,
                self.executor_id,
                status,
                updates=updates,
                failure_code=failure_code,
                failure_message=failure_message,
            )
        except LeaseLost as exc:
            # The outcome is known but cannot be written under this lease. Leaving it
            # EXECUTING is correct: reconciliation will find the order and repair it.
            log.error("could not resolve %s to %s: %s", request_id, status.value, exc)
            self.left_executing += 1
            return None

    # ── Inputs ────────────────────────────────────────────────────────────────

    def _settings(self) -> ExecutionSettings:
        """Read settings, failing closed if they cannot be read.

        A settings document that will not load must not mean "no limits" -- an
        unreadable ``trading_enabled`` becomes ``false``, which is what the default is.
        """
        try:
            settings = self.settings_provider()  # type: ignore[operator]
        except Exception:  # noqa: BLE001
            log.exception("could not read execution settings; failing closed")
            return ExecutionSettings()
        return settings or ExecutionSettings()

    def _market_state(self, symbol: str) -> MarketState:
        if self.market_state_provider is None:
            return MarketState.OPEN
        try:
            return self.market_state_provider(symbol)  # type: ignore[operator]
        except Exception:  # noqa: BLE001
            log.exception("could not determine market state for %s", symbol)
            return MarketState.UNKNOWN

    def _guard(
        self, request: TradeRequest, settings: ExecutionSettings, *, now: datetime
    ) -> GuardResult:
        """Gather a fresh broker snapshot and run the guard.

        The quote is read again here even though the confirmation carries one: the guard
        compares against the price we are about to trade at, not the price a human saw
        seconds ago (§41).
        """
        try:
            info = self.broker.symbol_info(request.symbol)
        except Exception:  # noqa: BLE001
            log.exception("symbol_info failed for %s", request.symbol)
            info = None
        try:
            quote = self.broker.quote(request.symbol)
        except Exception:  # noqa: BLE001
            log.exception("quote failed for %s", request.symbol)
            quote = None
        try:
            account = self.broker.account_info()
        except Exception:  # noqa: BLE001
            account = None
        try:
            positions = len(self.broker.open_positions())
        except Exception:  # noqa: BLE001
            positions = 0

        snapshot = BrokerSnapshot(
            symbol_info=info, quote=quote, account=account, open_positions=positions
        )
        return check(
            request,
            settings,
            market_state=self._market_state(request.symbol),
            broker=snapshot,
            allow_live_execution=self.allow_live_execution,
            now=now,
        )

    # ── 11A F-3: which account is this ────────────────────────────────────────

    def announce_account_mode(self) -> AccountMode:
        """Read the terminal's account mode once at startup and act on it.

        Called by ``main_executor`` before the listener starts. Three outcomes:

        * a demo or contest account, or the flag set: normal operation;
        * real money without the flag: ``reconcile_only``, one ops message, and every
          request refused by the guard's first rule -- startup reconciliation still runs,
          because a position that is already open needs watching whoever opened it;
        * the terminal will not say: treated as real money, for the reason
          ``AccountMode.is_real_money`` gives.

        The ops message is posted ONCE, here, rather than per refused request: an operator
        needs to know which terminal is open, and they need to know it before they send a
        request rather than a hundred times after.
        """
        try:
            self.account_mode = self.broker.account_info().mode
        except Exception:  # noqa: BLE001 - a broker that cannot say has not said "demo"
            log.exception("could not read the account mode; assuming real money")
            self.account_mode = AccountMode.UNKNOWN

        self.reconcile_only = (
            self.account_mode.is_real_money and not self.allow_live_execution
        )
        if self.reconcile_only:
            log.error(
                "executor on a %s account and AUREON_ALLOW_LIVE_EXECUTION is not 'true': "
                "execution disabled, reconciliation only",
                self.account_mode.value,
            )
            self._ops(
                "live_account_detected",
                f"executor on {self.account_mode.value.upper()} account — execution "
                "disabled. Set AUREON_ALLOW_LIVE_EXECUTION=true on the box if that is "
                "intended.",
            )
        elif self.account_mode.is_real_money:
            log.warning(
                "executor on a %s account with live execution ALLOWED",
                self.account_mode.value,
            )
            self._ops(
                "live_account_detected",
                f"executor on {self.account_mode.value.upper()} account with live "
                "execution ALLOWED — orders will use real money.",
            )
        return self.account_mode

    def _ops(self, name: str, detail: str) -> None:
        if self.on_ops_event is None:
            return
        try:
            self.on_ops_event(name, detail)  # type: ignore[operator]
        except Exception:  # noqa: BLE001 - an ops post must not stop the executor
            log.exception("ops event %s could not be posted", name)

    # ── Serving ───────────────────────────────────────────────────────────────

    def poll_once(self) -> int:
        """Process every CONFIRMED request. Returns how many were handled."""
        handled = 0
        for request in self.repository.list_by_status(TradeRequestStatus.CONFIRMED):
            if self._stop.is_set():
                break
            if self.process(request.request_id) is not None:
                handled += 1
        return handled

    def run(self) -> None:
        while not self._stop.is_set():
            if self.before_poll is not None:
                try:
                    self.before_poll()  # type: ignore[operator]
                except Exception:  # noqa: BLE001 - a side errand must not stop executing
                    log.exception("executor before_poll failed")
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001 - a poll failure must not kill the executor
                log.exception("executor poll failed")
            self._stop.wait(self._wait())

    def _wait(self) -> float:
        """This iteration's cadence, asked for each time round (11B)."""
        if self.pace is None:
            return self.poll_seconds
        try:
            return float(self.pace(self.poll_seconds))  # type: ignore[operator]
        except Exception:  # noqa: BLE001
            log.exception("executor pace hook failed; using the awake cadence")
            return self.poll_seconds

    def start(self) -> None:
        self._thread = threading.Thread(target=self.run, name="executor", daemon=True)
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

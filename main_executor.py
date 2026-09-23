#!/usr/bin/env python3
"""The executor process (§76).

The only process in Aureon that places orders.

## Startup order is a safety property (§76)

**Reconcile first, then serve.** A previous instance may have died mid-send, leaving a
request in ``EXECUTING`` with an order that may or may not exist at the broker. Serving
before reconciling would let a new claim be taken while that ambiguity is unresolved,
and the one thing this system must never do is place a second order for one
authorisation.

Reconciliation never sends. It only looks and records.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from datetime import datetime

from aureon.config import AureonConfig
from aureon.execution.broker_interface import BrokerInterface
from aureon.execution.control_worker import ControlWorker
from aureon.execution.execution_worker import ExecutionWorker
from aureon.execution.reconciliation_service import ReconciliationService
from aureon.models.base import utc_now
from aureon.models.enums import MarketState
from aureon.models.settings import ExecutionSettings
from aureon.services.heartbeat_service import HeartbeatService
from aureon.services.market_state_service import WeeklySchedule
from aureon.services.shutdown import flushed, install_handlers
from aureon.services.sleep_cycle import SleepCycle, SleepGate
from aureon.storage import paths
from aureon.storage.trade_request_repository import TradeRequestRepository

log = logging.getLogger("aureon.executor")


class Executor:
    """Reconciles, then serves CONFIRMED trade requests."""

    def __init__(
        self,
        config: AureonConfig,
        broker: BrokerInterface,
        repository: TradeRequestRepository,
        *,
        controls: object | None = None,
        settings_provider: object,
        market_state_provider: object | None = None,
        heartbeat: HeartbeatService | None = None,
        schedule: object | None = None,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self.config = config
        self.broker = broker
        self.repository = repository
        self.heartbeat = heartbeat

        # ── The weekend (11B) ────────────────────────────────────────────────
        #
        # The executor sleeps differently from the observer, and the difference is the
        # point: its request loop is never parked. A closed market is already refused, by
        # the guard, on this process's own broker -- and a human who confirms a trade on a
        # Saturday is owed that refusal rather than silence until Sunday night. What the
        # close changes here is only the cadence: the poll slows, and the heartbeat with
        # it, because the snapshot listener is what makes a CONFIRMED request prompt and
        # the poll is only its fallback.
        self.sleep = SleepCycle(
            schedule=schedule or WeeklySchedule(),
            close_confirm_seconds=config.close_confirm_seconds,
            preopen_seconds=config.preopen_seconds,
            sleep_heartbeat_seconds=config.sleep_heartbeat_seconds,
            sleep_poll_seconds=config.sleep_poll_seconds,
        )
        self.gate = SleepGate(
            cycle=self.sleep,
            states=self._market_states,
            # The SAME clock the market-state service reads, not this process's own. The
            # confirmation window is measured in differences between these readings while
            # the states it is confirming come from the provider's clock, so two clocks
            # would be measuring one condition against another's time.
            clock=now,
            on_sleep=self._on_cadence_change,
            on_wake=self._on_cadence_change,
            service=paths.SERVICE_EXECUTOR,
        )
        self._market_state_provider = market_state_provider

        self.worker = ExecutionWorker(
            repository,
            broker,
            magic=config.aureon_magic,
            settings_provider=settings_provider,
            market_state_provider=market_state_provider,
            lease_seconds=config.executor_lease_seconds,
            poll_seconds=config.executor_poll_seconds,
            allow_live_execution=config.allow_live_execution,
            before_poll=self.gate.tick,
            pace=self.gate.pace,
        )
        # Discord's cancels and closes arrive as control_requests and are performed
        # here, because this is the only process that holds a broker (§46, §47).
        self.controls = (
            ControlWorker(
                controls,  # type: ignore[arg-type]
                broker,
                executor_id=self.worker.executor_id,
                lease_seconds=config.executor_lease_seconds,
                poll_seconds=config.executor_poll_seconds,
            )
            if controls is not None
            else None
        )
        self.reconciliation = ReconciliationService(
            repository,
            broker,
            magic=config.aureon_magic,
            grace_seconds=config.reconcile_grace_seconds,
        )

    def _market_states(self) -> dict[str, MarketState]:
        """Every configured symbol's state, from the executor's own provider.

        A symbol the provider cannot answer for is UNKNOWN, which never sleeps. Without a
        provider the map is empty, which also never sleeps -- an executor that cannot tell
        whether the market is open must not slow itself down on a guess, and the guard is
        refusing those requests anyway.
        """
        if self._market_state_provider is None:
            return {}
        states: dict[str, MarketState] = {}
        for symbol in self.config.symbols:
            try:
                states[symbol] = self._market_state_provider(symbol)  # type: ignore[operator]
            except Exception:  # noqa: BLE001
                states[symbol] = MarketState.UNKNOWN
        return states

    def _on_cadence_change(self, crossing: object) -> None:
        """Slow the heartbeat at the close, restore it at the open.

        Slowed, never stopped: an executor whose heartbeat went quiet over the weekend
        would read as a dead executor, and the operator's Sunday evening would start with
        a false alarm instead of a market open.
        """
        if self.heartbeat is None:
            return
        try:
            self.heartbeat.set_interval(
                self.sleep.heartbeat_seconds(self.config.state_heartbeat_seconds)
            )
        except Exception:  # noqa: BLE001 - a cadence change must not stop executing
            log.exception("could not change the executor heartbeat cadence")

    def startup(self) -> int:
        """Connect, say which account this is, and reconcile.

        Returns how many requests were repaired or failed.
        """
        log.info("executor %s starting", self.worker.executor_id)
        self.broker.connect()

        # 11A F-3, and only wired here in 11B: this call is what sets ``reconcile_only``
        # and posts the one live-account ops message. Its own docstring said "called by
        # main_executor" while nothing called it, so on a real terminal the executor
        # announced nothing and AUREON_ALLOW_LIVE_EXECUTION -- the operator's deliberate
        # escape hatch -- was inert. After the connection and before reconciliation:
        # reconciliation runs whatever the account is, because a position already open
        # needs watching whoever opened it.
        self.worker.announce_account_mode()

        outcomes = self.reconciliation.reconcile_all()
        resolved = [o for o in outcomes if o.action in {"repaired", "failed"}]
        for outcome in outcomes:
            log.info(
                "reconciliation: %s -> %s%s",
                outcome.request_id,
                outcome.action,
                f" ({outcome.detail})" if outcome.detail else "",
            )
        if not outcomes:
            log.info("nothing to reconcile")

        if self.controls is not None:
            self.controls.start()
        if self.heartbeat is not None:
            self.heartbeat.start()
        return len(resolved)

    def run(self) -> None:
        self.startup()
        log.info("executor serving")
        try:
            self.worker.run()
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        log.info("executor shutting down")
        self.worker.stop()
        if self.controls is not None:
            self.controls.stop()
        if self.heartbeat is not None:
            self.heartbeat.stop()
        self.broker.close()
        flushed(paths.SERVICE_EXECUTOR)


def build_executor(config: AureonConfig) -> Executor:
    """Assemble a live executor from configuration."""
    from aureon.data.mt5_provider import MT5DataProvider
    from aureon.execution.mt5_broker import MT5Broker
    from aureon.services.market_state_service import MarketStateService
    from aureon.storage.runtime import build_storage

    broker = MT5Broker(
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
    settings_repository = storage.settings

    # Market state comes from a data provider, not the broker: the observer's notion of
    # OPEN/STALE is the one the rest of the system reports, so the executor must agree
    # with it rather than form a second opinion.
    provider = MT5DataProvider(market_tz=config.market_tz)
    market_state = MarketStateService(provider)

    return Executor(
        config,
        broker,
        storage.trade_requests,
        controls=storage.controls,
        settings_provider=settings_repository.read_or_default,
        market_state_provider=lambda symbol: market_state.state_for(symbol).state,
        # The same schedule object AND the same clock the state service uses, so the two
        # cannot disagree about when the market opens (11B).
        schedule=market_state.schedule,
        now=provider.now_utc,
        heartbeat=HeartbeatService(storage.heartbeats, paths.SERVICE_EXECUTOR),
    )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    config = AureonConfig.from_env()
    executor = build_executor(config)

    install_handlers(executor.worker.stop, service=paths.SERVICE_EXECUTOR)

    executor.run()
    return 0


# Re-exported so callers do not need to know where these live.
__all__ = ["Executor", "ExecutionSettings", "MarketState", "build_executor", "main"]


if __name__ == "__main__":
    sys.exit(main())

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
import signal
import sys
from types import FrameType

from aureon.config import AureonConfig
from aureon.execution.broker_interface import BrokerInterface
from aureon.execution.control_worker import ControlWorker
from aureon.execution.execution_worker import ExecutionWorker
from aureon.execution.reconciliation_service import ReconciliationService
from aureon.models.enums import MarketState
from aureon.models.settings import ExecutionSettings
from aureon.services.heartbeat_service import HeartbeatService
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
    ) -> None:
        self.config = config
        self.broker = broker
        self.repository = repository
        self.heartbeat = heartbeat

        self.worker = ExecutionWorker(
            repository,
            broker,
            magic=config.aureon_magic,
            settings_provider=settings_provider,
            market_state_provider=market_state_provider,
            lease_seconds=config.executor_lease_seconds,
            poll_seconds=config.executor_poll_seconds,
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

    def startup(self) -> int:
        """Connect and reconcile. Returns how many requests were repaired or failed."""
        log.info("executor %s starting", self.worker.executor_id)
        self.broker.connect()

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
            self.worker.start_listener()
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


def build_executor(config: AureonConfig) -> Executor:
    """Assemble a live executor from configuration."""
    from aureon.data.mt5_provider import MT5DataProvider
    from aureon.execution.mt5_broker import MT5Broker
    from aureon.services.market_state_service import MarketStateService
    from aureon.storage.control_request_repository import ControlRequestRepository
    from aureon.storage.firebase_service import get_client
    from aureon.storage.settings_repository import ExecutionSettingsRepository
    from aureon.storage.system_state_repository import HeartbeatRepository

    broker = MT5Broker(
        market_tz=config.market_tz,
        login=config.mt5_login,
        password=config.mt5_password,
        server=config.mt5_server,
        terminal_path=config.mt5_terminal_path,
    )
    client = get_client(
        project_id=config.firebase_project_id,
        emulator_host=config.firestore_emulator_host,
    )
    settings_repository = ExecutionSettingsRepository(client)

    # Market state comes from a data provider, not the broker: the observer's notion of
    # OPEN/STALE is the one the rest of the system reports, so the executor must agree
    # with it rather than form a second opinion.
    provider = MT5DataProvider(market_tz=config.market_tz)
    market_state = MarketStateService(provider)

    return Executor(
        config,
        broker,
        TradeRequestRepository(client),
        controls=ControlRequestRepository(client),
        settings_provider=settings_repository.read_or_default,
        market_state_provider=lambda symbol: market_state.state_for(symbol).state,
        heartbeat=HeartbeatService(
            HeartbeatRepository(client), paths.SERVICE_EXECUTOR
        ),
    )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    config = AureonConfig.from_env()
    executor = build_executor(config)

    def handle(signum: int, _frame: FrameType | None) -> None:
        log.info("received signal %s", signum)
        executor.worker.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, handle)

    executor.run()
    return 0


# Re-exported so callers do not need to know where these live.
__all__ = ["Executor", "ExecutionSettings", "MarketState", "build_executor", "main"]


if __name__ == "__main__":
    sys.exit(main())

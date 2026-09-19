#!/usr/bin/env python3
"""The position monitor process (§77, §58).

Records what positions actually did. It places no orders -- the only broker calls it makes
are reads, plus nothing else.

## Startup order (§77)

Load stored OPEN trades and PENDING requests → query the broker → reconcile → serve. The
gap is the interesting part: while this process was down a position may have closed, a
pending order may have filled, and a human may have opened something by hand. Serving
first would leave all three unrecorded until they happened to change again.

## It runs regardless of ``trading_enabled`` (§58)

That switch stops the executor. Positions opened before it was thrown are still live and
still need their closes recorded -- a monitor that paused alongside the executor would
blind the operator during exactly the incident that made them disable trading.
"""

from __future__ import annotations

import logging
import signal
import sys
from datetime import datetime, timedelta
from types import FrameType

from aureon.config import AureonConfig
from aureon.execution.broker_interface import BrokerInterface
from aureon.models.enums import Timeframe
from aureon.positions.position_monitor import PositionMonitor
from aureon.services.heartbeat_service import HeartbeatService
from aureon.storage import paths
from aureon.storage.trade_repository import TradeRepository
from aureon.storage.trade_request_repository import TradeRequestRepository

log = logging.getLogger("aureon.monitor")

# How far back to pull M1 candles when rebuilding excursions for a position that was open
# while the monitor was down (§45).
RECONSTRUCT_LOOKBACK_HOURS = 48.0


class Monitor:
    """Wires the position-monitoring pipeline together."""

    def __init__(
        self,
        config: AureonConfig,
        broker: BrokerInterface,
        trades: TradeRepository,
        requests: TradeRequestRepository,
        *,
        candle_provider: object | None = None,
        heartbeat: HeartbeatService | None = None,
        point: float = 0.01,
    ) -> None:
        self.config = config
        self.broker = broker
        self.trades = trades
        self.candle_provider = candle_provider
        self.heartbeat = heartbeat

        self.monitor = PositionMonitor(
            trades,
            requests,
            broker,
            magic=config.aureon_magic,
            account_scope=config.account_scope,
            market_tz=config.market_tz,
            point=point,
            poll_seconds=config.monitor_poll_seconds,
        )

    def startup(self, *, now: datetime | None = None) -> None:
        log.info("monitor starting (scope=%s)", self.config.account_scope)
        self.broker.connect()

        # Rebuild excursions for anything that was open while we were down, BEFORE the
        # first poll: once live quotes start arriving the offline stretch is lost.
        self._reconstruct_offline_excursions(now=now)

        result = self.monitor.startup(now=now)
        log.info(
            "startup reconciliation: %d opened, %d closed, %d partial, %d external, "
            "%d pending resolved",
            len(result.opened),
            len(result.closed),
            len(result.partially_closed),
            len(result.imported_external),
            len(result.pending_resolved),
        )
        if self.heartbeat is not None:
            self.heartbeat.start()

    def _reconstruct_offline_excursions(self, *, now: datetime | None = None) -> None:
        """Rebuild excursions from M1 candles for positions we did not watch (§45).

        Only for trades whose stored excursion is empty -- a position already carrying
        live-tick figures must not have them overwritten by the coarser reconstruction.
        """
        if self.candle_provider is None:
            return
        for trade in self.trades.open_trades():
            if trade.excursion.mfe is not None:
                continue
            try:
                start = trade.open_time.utc
                end = now or self.broker.quote(trade.symbol).captured_at
                if (end - start) > timedelta(hours=RECONSTRUCT_LOOKBACK_HOURS):
                    start = end - timedelta(hours=RECONSTRUCT_LOOKBACK_HOURS)
                candles = self.candle_provider.get_closed_candles(  # type: ignore[attr-defined]
                    trade.symbol, Timeframe.M1, start, end
                )
                if candles:
                    self.monitor.reconstruct_excursions(trade, candles, until=end)
                    log.info(
                        "reconstructed excursions for %s from %d M1 candle(s)",
                        trade.trade_id,
                        len(candles),
                    )
            except Exception:  # noqa: BLE001 - diagnostic, never critical
                log.exception("could not reconstruct excursions for %s", trade.trade_id)

    def run(self) -> None:
        self.startup()
        log.info("monitor serving (independent of trading_enabled, per §58)")
        try:
            self.monitor.run()
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        log.info("monitor shutting down")
        self.monitor.stop()
        if self.heartbeat is not None:
            self.heartbeat.stop()
        self.broker.close()


def build_monitor(config: AureonConfig) -> Monitor:
    """Assemble a live monitor from configuration."""
    from aureon.data.mt5_provider import MT5DataProvider
    from aureon.execution.mt5_broker import MT5Broker
    from aureon.storage.firebase_service import get_client
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
    provider = MT5DataProvider(market_tz=config.market_tz)

    return Monitor(
        config,
        broker,
        TradeRepository(client, account_scope=config.account_scope),
        TradeRequestRepository(client),
        candle_provider=provider,
        heartbeat=HeartbeatService(HeartbeatRepository(client), paths.SERVICE_MONITOR),
    )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    config = AureonConfig.from_env()
    monitor = build_monitor(config)

    def handle(signum: int, _frame: FrameType | None) -> None:
        log.info("received signal %s", signum)
        monitor.monitor.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, handle)

    monitor.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())

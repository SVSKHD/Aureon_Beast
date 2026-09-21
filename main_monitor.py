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
from collections.abc import Callable
from datetime import datetime, timedelta
from types import FrameType

from aureon.config import AureonConfig
from aureon.execution.broker_interface import BrokerInterface
from aureon.models.base import utc_now
from aureon.models.enums import MarketState, Timeframe
from aureon.positions.position_monitor import PositionMonitor
from aureon.services.heartbeat_service import HeartbeatService
from aureon.services.market_state_service import WeeklySchedule
from aureon.services.sleep_cycle import SleepCycle, SleepGate
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
        market_state_provider: object | None = None,
        schedule: object | None = None,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self.config = config
        self.broker = broker
        self.trades = trades
        self.candle_provider = candle_provider
        self.heartbeat = heartbeat

        # ── The weekend (11B) ────────────────────────────────────────────────
        #
        # The monitor IS parked, unlike the executor: every number it records comes from a
        # quote, and a closed market has no new ones -- polling would rewrite the same
        # excursions with the same prices for forty-eight hours. What makes that safe is
        # the ONE full reconciliation at the close, which is also the most useful moment
        # for it: whatever the broker's books say after the last tick of the week is what
        # they will still say on Sunday, so a discrepancy found now has two days to be
        # looked at rather than being noticed at the open.
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
            # The provider's clock, not this process's: see the note in main_executor.
            clock=now,
            on_sleep=self._on_market_close,
            on_wake=self._on_market_open,
            service=paths.SERVICE_MONITOR,
        )
        self._market_state_provider = market_state_provider
        #: How many closes have been reconciled, for the emulator test to count.
        self.closing_reconciliations = 0

        self.monitor = PositionMonitor(
            trades,
            requests,
            broker,
            magic=config.aureon_magic,
            account_scope=config.account_scope,
            market_tz=config.market_tz,
            point=point,
            poll_seconds=config.monitor_poll_seconds,
            before_poll=self.gate.tick,
            parked=lambda: self.gate.parked,
            pace=self.gate.pace,
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

    def _market_states(self) -> dict[str, MarketState]:
        """Every configured symbol's state. A fault reads UNKNOWN, which never sleeps."""
        if self._market_state_provider is None:
            return {}
        states: dict[str, MarketState] = {}
        for symbol in self.config.symbols:
            try:
                states[symbol] = self._market_state_provider(symbol)  # type: ignore[operator]
            except Exception:  # noqa: BLE001
                states[symbol] = MarketState.UNKNOWN
        return states

    def _on_market_close(self, crossing: object) -> None:
        """One full reconciliation, then park.

        The same ``startup`` reconciliation the process runs when it boots, deliberately:
        a second "closing" variant would be a second definition of what reconciled means,
        and the two would drift. It is the last chance to compare our books against the
        broker's while the broker is still answering; anything found after this is found at
        the open, in the busiest ten minutes of the week.
        """
        try:
            result = self.monitor.startup()
            self.closing_reconciliations += 1
            log.info(
                "closing reconciliation: %d opened, %d closed, %d partial, %d external, "
                "%d pending resolved",
                len(result.opened),
                len(result.closed),
                len(result.partially_closed),
                len(result.imported_external),
                len(result.pending_resolved),
            )
        except Exception:  # noqa: BLE001 - a failed reconciliation must not stop the park
            log.exception("the closing reconciliation failed")
        self._set_heartbeat_cadence()

    def _on_market_open(self, crossing: object) -> None:
        self._set_heartbeat_cadence()

    def _set_heartbeat_cadence(self) -> None:
        if self.heartbeat is None:
            return
        try:
            self.heartbeat.set_interval(
                self.sleep.heartbeat_seconds(self.config.state_heartbeat_seconds)
            )
        except Exception:  # noqa: BLE001
            log.exception("could not change the monitor heartbeat cadence")

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
    from aureon.services.market_state_service import MarketStateService
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
    # The same market-state service the observer and executor use, and its schedule, so
    # three processes cannot hold three opinions about when the week ends (11B).
    market_state = MarketStateService(provider)

    return Monitor(
        config,
        broker,
        TradeRepository(client, account_scope=config.account_scope),
        TradeRequestRepository(client),
        candle_provider=provider,
        heartbeat=HeartbeatService(HeartbeatRepository(client), paths.SERVICE_MONITOR),
        market_state_provider=lambda symbol: market_state.state_for(symbol).state,
        schedule=market_state.schedule,
        now=provider.now_utc,
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

"""Aureon cTrader-native observer.

Run this as a Python cBot on XAUUSD M5 and XAGUSD M5 (one instance per symbol).
It NEVER places, modifies or closes orders. cTrader owns the market feed; Aureon owns
observation and notifications.

Discord credentials are read from the local process environment and must never be
hard-coded into this file.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import requests


BROKER = "CTRADER"
DISCORD_API = "https://discord.com/api/v10"


class AureonCTrader:
    """Thin cTrader lifecycle shell for the Aureon intelligence stack."""

    def __init__(self):
        self.started_at = None
        self.symbol = None
        self.timeframe = None
        self._last_closed_time = None

    def on_start(self):
        self.started_at = datetime.now(timezone.utc)
        self.symbol = str(api.SymbolName)
        self.timeframe = str(api.TimeFrame)

        api.Print(
            f"Aureon cTrader started | {self.symbol} | {self.timeframe} | "
            "OBSERVATION ONLY"
        )
        self._discord(
            "🟢 **AUREON cTrader STARTED**\n"
            f"**Broker:** {BROKER}\n"
            f"**Symbol:** {self.symbol}\n"
            f"**Timeframe:** {self.timeframe}\n"
            "**Mode:** Observation / notifications only\n"
            "Aureon is watching the cTrader feed."
        )

    def on_tick(self):
        # Keep tick handling intentionally light. Journey/agent evaluation belongs on
        # deterministic snapshots; future pre-cross pressure can consume tick state here.
        return

    def on_bar_closed(self):
        """Receive a completed cTrader bar without ever touching order APIs."""
        bar = api.Bars.Last(0)
        opened = str(bar.OpenTime)
        if opened == self._last_closed_time:
            return
        self._last_closed_time = opened

        candle = {
            "broker": BROKER,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "open_time": opened,
            "open": float(bar.Open),
            "high": float(bar.High),
            "low": float(bar.Low),
            "close": float(bar.Close),
            "tick_volume": float(bar.TickVolume),
        }

        # Native cTrader feed boundary. The next mapping layer feeds this normalized
        # candle into the SAME Aureon agent/V4 contracts used by MT5.
        self._observe(candle)

    def on_stop(self):
        api.Print(f"Aureon cTrader stopped | {self.symbol} | {self.timeframe}")
        self._discord(
            "🔴 **AUREON cTrader STOPPED**\n"
            f"**Symbol:** {self.symbol}\n"
            f"**Timeframe:** {self.timeframe}"
        )

    def on_exception(self, exception):
        api.Print(f"Aureon cTrader error: {exception}")
        self._discord(
            "⚠️ **AUREON cTrader ERROR**\n"
            f"**Symbol:** {self.symbol or 'unknown'}\n"
            f"`{type(exception).__name__}: {exception}`"
        )

    def _observe(self, candle: dict) -> None:
        """cTrader -> Aureon observation boundary.

        This intentionally contains no trading operation. Agent/V4 mapping is attached
        here so both XAUUSD and XAGUSD use the same implementation independently.
        """
        api.Print(
            "Aureon closed bar | "
            f"{candle['symbol']} {candle['timeframe']} "
            f"O={candle['open']} H={candle['high']} "
            f"L={candle['low']} C={candle['close']}"
        )

    def _discord(self, message: str) -> None:
        token = os.getenv("AUREON_CTRADER_DISCORD_TOKEN", "").strip()
        channel_id = os.getenv("AUREON_CTRADER_ALERT_CHANNEL_ID", "").strip()
        if not token or not channel_id:
            api.Print(
                "Aureon Discord notification skipped: "
                "AUREON_CTRADER_DISCORD_TOKEN/channel not configured"
            )
            return

        try:
            response = requests.post(
                f"{DISCORD_API}/channels/{channel_id}/messages",
                headers={
                    "Authorization": f"Bot {token}",
                    "Content-Type": "application/json",
                },
                json={"content": message},
                timeout=8,
            )
            if response.status_code not in (200, 201):
                api.Print(
                    f"Aureon Discord notification failed: HTTP {response.status_code}"
                )
        except Exception as exc:
            # Notification failure must never kill market observation.
            api.Print(f"Aureon Discord notification error: {exc}")

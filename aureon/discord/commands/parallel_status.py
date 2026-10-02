"""Read-only Discord diagnostics for the parallel symbol observer."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import discord
from discord import app_commands

from aureon.discord.bot import requires_authorization
from aureon.discord.context import BotContext
from aureon.discord.embeds import notice_embed


def _age_seconds(value: object) -> float | None:
    if not value:
        return None
    try:
        at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return max(0.0, (datetime.now(timezone.utc) - at.astimezone(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return None


def _seconds(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}s"


class ParallelStatusCommands:
    def __init__(self, context: BotContext) -> None:
        self.context = context

    @requires_authorization
    async def parallel_status(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        heartbeat = await self.context.run(
            self.context.heartbeats.read, "observer"
        )
        if heartbeat is None:
            await interaction.followup.send(
                embed=notice_embed("Parallel status unavailable", "Observer heartbeat not found.", bad=True),
                ephemeral=True,
            )
            return

        detail = dict(heartbeat.detail or {})
        enabled = bool(detail.get("parallel_enabled"))
        streams = detail.get("streams") if isinstance(detail.get("streams"), dict) else {}
        expected = len(self.context.config.symbols) * len(self.context.config.timeframes)
        healthy = sum(
            1 for value in streams.values()
            if isinstance(value, dict) and value.get("status") == "RUNNING"
        )
        mode = "🟢 PARALLEL" if enabled else "🟡 SEQUENTIAL"
        lines = [
            f"**Mode:** {mode}",
            f"**Broker:** {detail.get('broker') or self.context.config.broker_source}",
            f"**Workers:** {healthy}/{expected} reporting",
            f"**Provider access:** {detail.get('provider_access', 'unknown')}",
        ]

        for symbol in self.context.config.symbols:
            for timeframe in self.context.config.timeframes:
                key = f"{symbol}/{timeframe.value}"
                stream = streams.get(key, {}) if isinstance(streams, dict) else {}
                state = await self.context.run(
                    self.context.system_state.read_symbol, symbol, timeframe
                )
                symbol_state = state.symbols[0] if state is not None and state.symbols else None
                quote = getattr(symbol_state, "last_quote", None)
                bid = stream.get("bid") if isinstance(stream, dict) else None
                ask = stream.get("ask") if isinstance(stream, dict) else None
                quote_at = stream.get("quote_at") if isinstance(stream, dict) else None
                if quote is not None:
                    bid, ask, quote_at = quote.bid, quote.ask, quote.captured_at.isoformat()
                processing = stream.get("processing_ms") if isinstance(stream, dict) else None
                candle_at = (
                    stream.get("last_closed_candle")
                    or stream.get("last_candle_open")
                    if isinstance(stream, dict) else None
                )
                lines.extend([
                    "",
                    f"**{symbol} · {timeframe.value}**",
                    f"Worker: {'🟢 RUNNING' if stream.get('status') == 'RUNNING' else '⚪ WAITING'}",
                    f"Bid / Ask: {bid if bid is not None else '—'} / {ask if ask is not None else '—'}",
                    f"Tick age: {_seconds(_age_seconds(quote_at))}",
                    f"Processing: {f'{float(processing):.2f} ms' if processing is not None else '—'}",
                    f"Last candle: {candle_at or '—'}",
                    f"Candle age: {_seconds(_age_seconds(candle_at))}",
                ])

        heartbeat_age = max(
            0.0,
            (datetime.now(timezone.utc) - heartbeat.updated_at.astimezone(timezone.utc)).total_seconds(),
        )
        lines.extend(["", f"Observer diagnostic age: **{heartbeat_age:.1f}s**"])
        embed = discord.Embed(
            title="⚡ Aureon Parallel Status",
            description="\n".join(lines),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


def register(tree: Any, context: BotContext) -> None:
    commands = ParallelStatusCommands(context)

    @tree.command(
        name="parallel-status",
        description="Gold/Silver parallel worker health and latency",
    )
    async def parallel_status(interaction: discord.Interaction) -> None:
        await commands.parallel_status(interaction)

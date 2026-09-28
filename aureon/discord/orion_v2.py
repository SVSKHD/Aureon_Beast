"""Orion V2 deterministic market-context Discord card.

This module is intentionally additive. It does not replace or modify Aureon's existing
setup/notification embeds and contains no execution path. Callers supply already-computed
agent/context facts; this module only renders them for human review.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

COLOUR_INFO = 0x1565C0


@dataclass(frozen=True)
class OrionV2Context:
    symbol: str
    timeframe: str
    direction: str
    state: str
    price: float | None = None
    setup_zone: str = "—"
    ema_m5: str = "—"
    trend_m15: str = "—"
    trend_h1: str = "—"
    trend_h4: str = "—"
    rsi: str = "—"
    session: str = "—"
    regime: str = "—"
    volume: str = "—"
    liquidity: str = "—"
    wick_rejection: str = "—"
    structure: str = "—"
    progress: tuple[tuple[str, bool | None], ...] = field(default_factory=tuple)
    move_levels: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    invalidation: str = "—"
    status: str = "SCANNING"
    next_step: str = "Waiting for deterministic setup evidence."
    updated: str = "—"


def _mark(value: bool | None) -> str:
    if value is True:
        return "✅"
    if value is False:
        return "❌"
    return "⏳"


def orion_v2_embed(card: OrionV2Context) -> Any:
    """Render Orion V2 without changing any existing Aureon embed."""
    import discord

    title = f"ORION V2 • {card.symbol} • {card.timeframe}"
    description = f"**{card.direction.upper()} — {card.state.upper()}**"
    embed = discord.Embed(title=title, description=description, colour=COLOUR_INFO)

    price = "—" if card.price is None else f"{card.price:g}"
    embed.add_field(
        name="MARKET",
        value=f"**Price:** {price}\n**Setup zone:** {card.setup_zone}",
        inline=False,
    )
    embed.add_field(
        name="CONTEXT",
        value=(
            f"**M5 EMA 20/50:** {card.ema_m5}\n"
            f"**M15:** {card.trend_m15}  ·  **H1:** {card.trend_h1}  ·  **H4:** {card.trend_h4}\n"
            f"**RSI:** {card.rsi}\n"
            f"**Session:** {card.session}  ·  **Regime:** {card.regime}\n"
            f"**Volume:** {card.volume}\n"
            f"**Liquidity:** {card.liquidity}\n"
            f"**Wick / rejection:** {card.wick_rejection}\n"
            f"**Structure:** {card.structure}"
        ),
        inline=False,
    )

    if card.progress:
        embed.add_field(
            name="SETUP PROGRESS",
            value="\n".join(f"{_mark(done)} {name}" for name, done in card.progress),
            inline=False,
        )

    if card.move_levels:
        embed.add_field(
            name="MOVE MAP",
            value="\n".join(f"**{label}:** {level}" for label, level in card.move_levels),
            inline=False,
        )

    embed.add_field(name="INVALIDATION", value=card.invalidation, inline=False)
    embed.add_field(
        name=f"ORION STATUS · {card.status.upper()}",
        value=f"**Next:** {card.next_step}\n\nManual review only. Orion V2 does not execute trades.",
        inline=False,
    )
    embed.set_footer(text=f"Orion V2 · {card.updated}")
    return embed

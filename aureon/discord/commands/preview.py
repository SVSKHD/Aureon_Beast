"""Synthetic notification previews using the production renderer."""

from __future__ import annotations

from typing import Any, Literal

import discord
from discord import app_commands

from aureon.discord.bot import requires_authorization
from aureon.discord.context import BotContext
from aureon.discord.embeds import notification_embed
from aureon.discord.service import NotificationScreen

PreviewStyle = Literal["all", "bullish", "bearish", "neutral"]


def preview_embeds(style: PreviewStyle = "all") -> list[discord.Embed]:
    """Fixed examples only: no market reads, persistence or execution controls."""
    scenarios = {
        "bullish": ("buy", "UP", "4187.35 / 4187.27 · fast_above"),
        "bearish": ("sell", "DOWN", "4187.27 / 4187.35 · fast_below"),
        "neutral": (None, "FLAT", "4187.30 / 4187.30 · equal"),
    }
    selected = scenarios if style == "all" else {style: scenarios[style]}
    embeds = []
    for name, (side, trend, ema) in selected.items():
        screen = NotificationScreen(
            title=f"TEST · XAUUSD · EMA 20/50 · {name.upper()}",
            symbol="XAUUSD",
            detection_id=f"test-{name}",
            side=side,
            fields=[
                ("Price", "4190.28"),
                ("Cross", "EMA20 / EMA50"),
                ("Session", "london"),
                ("Trend", trend),
                ("Quality", "WEAK"),
                ("EMA20 / EMA50", ema),
                ("EMA200 context", "ema200_unavailable"),
                ("Pattern", "GRADUAL_MOMENTUM_SHIFT" if side else "CONSOLIDATION"),
                (
                    "Agent consensus",
                    "█░░░░░░░░░  INSUFFICIENT\n"
                    "✅ 1 supportive · ➖ 2 neutral · ❌ 0 conflicting",
                ),
            ],
        )
        embed = notification_embed(screen)
        embed.set_footer(text="TEST PREVIEW · synthetic data · no trade or live signal")
        embeds.append(embed)
    return embeds


class PreviewCommands:
    def __init__(self, context: BotContext) -> None:
        self.context = context

    @requires_authorization
    async def preview(
        self,
        interaction: discord.Interaction,
        style: PreviewStyle = "all",
        public: bool = False,
    ) -> None:
        await interaction.response.send_message(
            content="**Layout test — example data only.**",
            embeds=preview_embeds(style),
            ephemeral=not public,
            allowed_mentions=discord.AllowedMentions.none(),
        )


def register(tree: Any, context: BotContext) -> None:
    commands = PreviewCommands(context)

    @tree.command(name="test", description="Preview alert layouts with synthetic data")
    @app_commands.describe(
        style="Preview all three colours or choose one",
        public="Show in this channel; default is visible only to you",
    )
    async def test_message(
        interaction: discord.Interaction,
        style: PreviewStyle = "all",
        public: bool = False,
    ) -> None:
        await commands.preview(interaction, style, public)

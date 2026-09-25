"""Read-only EOD training-memory status."""

from __future__ import annotations

import logging
from typing import Any

import discord
from discord import app_commands

from aureon.discord.bot import requires_authorization
from aureon.discord.context import BotContext
from aureon.discord.embeds import notice_embed

log = logging.getLogger(__name__)


def training_status_embed(status: Any) -> discord.Embed:
    if status is None:
        return notice_embed(
            "Training status",
            "No completed EOD training memory has been written yet.",
        )

    lines = [
        f"**Date:** {status.market_date}",
        f"**Symbol:** {status.symbol}",
        f"**Examples:** {status.examples_written}",
        f"**Reached +$6:** {status.reached_six}",
        f"**Did not reach +$6 by EOD:** {status.not_reached_six}",
        f"**Unavailable labels:** {status.unavailable_six}",
        f"**Complete evaluations:** {status.complete_evaluations}",
        f"**Pre-$6 MAE available:** {status.mae_before_six_available}",
        "",
        "**By timeframe**",
    ]
    for one in status.by_timeframe:
        lines.append(
            f"`{one.timeframe.value}` setups {one.setups} · "
            f"+$6 {one.reached_six} · "
            f"no +$6 {one.not_reached_six} · "
            f"eval {one.complete_evaluations} · "
            f"pre-$6 MAE {one.mae_before_six_available}"
        )

    lines.extend(
        [
            "",
            f"Feature schema: `{status.feature_schema_version}`",
            f"Label schema: `{status.label_schema_version}`",
            "Research memory only · not an execution signal",
        ]
    )
    return notice_embed("🧠 EOD training status", "\n".join(lines))


class TrainingStatusCommands:
    def __init__(self, context: BotContext) -> None:
        self.context = context

    @requires_authorization
    async def training_status(
        self,
        interaction: discord.Interaction,
        symbol: str | None = None,
    ) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        if self.context.training_memory is None:
            await interaction.followup.send(
                embed=notice_embed(
                    "Training status unavailable",
                    "This build has no training-memory reader configured.",
                    bad=True,
                ),
                ephemeral=True,
            )
            return

        symbol = (symbol or self.context.config.symbols[0]).upper()
        if symbol not in self.context.config.symbols:
            await interaction.followup.send(
                embed=notice_embed(
                    "No such symbol here",
                    f"{symbol} is not configured in this Aureon instance.",
                    bad=True,
                ),
                ephemeral=True,
            )
            return

        try:
            status = await self.context.run(
                self.context.training_memory.latest_status,
                symbol,
            )
        except Exception:  # noqa: BLE001 - read failure must be visible to the operator
            log.exception("/training-status failed")
            await interaction.followup.send(
                embed=notice_embed(
                    "Training status unavailable",
                    "Could not read the EOD training-memory status from SQLite.",
                    bad=True,
                ),
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            embed=training_status_embed(status),
            ephemeral=True,
        )


def register(tree: Any, context: BotContext) -> None:
    commands = TrainingStatusCommands(context)

    @tree.command(
        name="training-status",
        description="Latest EOD training-memory status",
    )
    @app_commands.describe(symbol="Show one configured symbol")
    @app_commands.choices(
        symbol=[
            app_commands.Choice(name=name, value=name)
            for name in context.config.symbols
        ]
    )
    async def training_status(
        interaction: discord.Interaction,
        symbol: str | None = None,
    ) -> None:
        await commands.training_status(interaction, symbol)

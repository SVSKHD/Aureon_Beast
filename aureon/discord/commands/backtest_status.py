"""Read-only latest walk-forward backtest status."""

from __future__ import annotations

from typing import Any

import discord
from discord import app_commands

from aureon.discord.bot import requires_authorization
from aureon.discord.context import BotContext
from aureon.discord.embeds import notice_embed


def _fmt(value: float | None) -> str:
    return "—" if value is None else f"{value:.3f}"


def backtest_status_embed(backtest: Any) -> discord.Embed:
    if backtest is None:
        return notice_embed(
            "📊 Backtest status",
            "No walk-forward backtest has been stored yet.",
        )

    lines = [
        f"**Backtest:** `{backtest.backtest_id}`",
        f"**Status:** `{backtest.status}`",
        f"**Model:** `{backtest.model_id or 'unassigned'}`",
        f"**Period:** {backtest.start_market_date or '—'} → "
        f"{backtest.end_market_date or '—'}",
        f"**Folds:** {len(backtest.folds)}",
        f"**Out-of-sample predictions:** {backtest.out_of_sample_predictions}",
        "",
        "**Out-of-sample metrics**",
    ]
    for target in ("six", "twenty", "forty"):
        metric = backtest.aggregate_metrics.get(target)
        if metric is None:
            lines.append(f"`{target}` — insufficient data")
            continue
        lines.append(
            f"`{target}` n {metric.samples} · AUC {_fmt(metric.roc_auc)} · "
            f"Brier {_fmt(metric.brier)} · precision {_fmt(metric.precision)} · "
            f"recall {_fmt(metric.recall)}"
        )
    if backtest.failure_message:
        lines.extend(["", f"**Note:** {backtest.failure_message}"])
    lines.extend(["", "Chronological walk-forward only · no random split"])
    return notice_embed("📊 Backtest status", "\n".join(lines))


class BacktestStatusCommands:
    def __init__(self, context: BotContext) -> None:
        self.context = context

    @requires_authorization
    async def backtest_status(
        self,
        interaction: discord.Interaction,
        symbol: str | None = None,
    ) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        if self.context.models is None:
            await interaction.followup.send(
                embed=notice_embed("Backtest unavailable", "No model reader configured."),
                ephemeral=True,
            )
            return

        symbol = (symbol or self.context.config.symbols[0]).upper()
        backtest = await self.context.run(self.context.models.latest_backtest, symbol)
        await interaction.followup.send(
            embed=backtest_status_embed(backtest),
            ephemeral=True,
        )


def register(tree: Any, context: BotContext) -> None:
    commands = BacktestStatusCommands(context)

    @tree.command(
        name="backtest-status",
        description="Latest chronological model backtest status",
    )
    @app_commands.describe(symbol="Show one configured symbol")
    @app_commands.choices(
        symbol=[
            app_commands.Choice(name=name, value=name)
            for name in context.config.symbols
        ]
    )
    async def backtest_status(
        interaction: discord.Interaction,
        symbol: str | None = None,
    ) -> None:
        await commands.backtest_status(interaction, symbol)

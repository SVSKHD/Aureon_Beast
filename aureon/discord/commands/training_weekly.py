"""Read-only weekly detector movement-ladder report."""

from __future__ import annotations

import logging
from typing import Any

import discord
from discord import app_commands

from aureon.discord.bot import requires_authorization
from aureon.discord.context import BotContext
from aureon.discord.embeds import notice_embed
from aureon.models.base import utc_now
from aureon.reviews.periods import previous_iso_week
from aureon.services.weekly_training_report import build_weekly_training_report

log = logging.getLogger(__name__)


def weekly_training_embed(report: Any) -> discord.Embed:
    lines = [
        f"**Week:** {report.iso_year}-W{report.iso_week:02d}",
        f"**Symbol:** {report.symbol}",
        f"**Setup examples:** {report.examples}",
        "",
        "**Detector / timeframe movement**",
    ]
    for row in report.rows:
        median_max = (
            f"${row.median_max_move_price:.1f}"
            if row.median_max_move_price is not None
            else "—"
        )
        max_move = (
            f"${row.maximum_move_price:.1f}"
            if row.maximum_move_price is not None
            else "—"
        )
        after_six = (
            f"${row.median_extension_after_six_price:.1f}"
            if row.median_extension_after_six_price is not None
            else "—"
        )
        lines.append(
            f"`{row.agent_name}/{row.timeframe.value}` · "
            f"decisions {row.decisions} · aligned {row.aligned_decisions} · "
            f"$6 {row.reached_six} · $20 {row.reached_twenty} · "
            f"$40 {row.reached_forty} · median max {median_max} · "
            f"max {max_move} · median after $6 {after_six}"
        )
    if not report.rows:
        lines.append("No directional agent decisions stored for this completed week.")
    lines.extend(
        [
            "",
            "Movement outcomes are credited only when that detector was aligned with the "
            "setup direction. Opposed decisions remain counted separately.",
            "Research memory only · not an execution signal",
        ]
    )
    return notice_embed("🧠 Weekly movement report", "\n".join(lines))


class TrainingWeeklyCommands:
    def __init__(self, context: BotContext) -> None:
        self.context = context

    @requires_authorization
    async def training_weekly(
        self,
        interaction: discord.Interaction,
        symbol: str | None = None,
    ) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        if self.context.training_memory is None:
            await interaction.followup.send(
                embed=notice_embed(
                    "Weekly training unavailable",
                    "This build has no training-memory reader configured.",
                    bad=True,
                ),
                ephemeral=True,
            )
            return

        symbol = (symbol or self.context.config.symbols[0]).upper()
        year, week = previous_iso_week(
            self.context.config.market_tz,
            now=utc_now(),
        )
        try:
            report = await self.context.run(
                build_weekly_training_report,
                self.context.training_memory,
                symbol=symbol,
                iso_year=year,
                iso_week=week,
            )
        except Exception:  # noqa: BLE001
            log.exception("/training-weekly failed")
            await interaction.followup.send(
                embed=notice_embed(
                    "Weekly training unavailable",
                    "Could not build the weekly movement report from SQLite.",
                    bad=True,
                ),
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            embed=weekly_training_embed(report),
            ephemeral=True,
        )


def register(tree: Any, context: BotContext) -> None:
    commands = TrainingWeeklyCommands(context)

    @tree.command(
        name="training-weekly",
        description="Weekly $6/$20/$40 detector movement report",
    )
    @app_commands.describe(symbol="Show one configured symbol")
    @app_commands.choices(
        symbol=[
            app_commands.Choice(name=name, value=name)
            for name in context.config.symbols
        ]
    )
    async def training_weekly(
        interaction: discord.Interaction,
        symbol: str | None = None,
    ) -> None:
        await commands.training_weekly(interaction, symbol)

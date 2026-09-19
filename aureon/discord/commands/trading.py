"""``/trading status|disable|enable`` (§57).

The asymmetry is the design: **disabling is instant, enabling needs a confirmation.**

Disabling is the safe direction and the one reached for during an incident, so it writes
``settings/execution.trading_enabled=false`` and its audit row immediately -- no button, no
second step. Enabling arms real money, so it shows what that means and requires a press.
"""

from __future__ import annotations

import logging
from typing import Any

import discord
from discord import app_commands

from aureon.discord.bot import requires_authorization
from aureon.discord.context import BotContext
from aureon.discord.embeds import notice_embed
from aureon.discord.service import ENABLE_DETAILS, trading_change_summary
from aureon.discord.views import EnableTradingView

log = logging.getLogger(__name__)


class TradingCommands:
    def __init__(self, context: BotContext) -> None:
        self.context = context

    @requires_authorization
    async def status(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        settings = await self.context.execution_settings()
        detail = "🟢 **enabled** — confirmed requests will be sent to the broker."
        if not settings.trading_enabled:
            detail = "🔴 **disabled** — confirmed requests will be refused."
            if settings.disabled_reason:
                detail += f"\nReason: {settings.disabled_reason}"
        if settings.updated_by:
            detail += f"\nLast changed by {settings.updated_by}"
        await interaction.followup.send(
            embed=notice_embed("Trading", detail), ephemeral=True
        )

    @requires_authorization
    async def disable(
        self, interaction: discord.Interaction, reason: str | None = None
    ) -> None:
        """Immediate, deliberately. No confirmation step (§57)."""
        await interaction.response.defer(thinking=True, ephemeral=True)
        actor = str(interaction.user.id)
        await self.context.run(
            self.context.settings.set_trading_enabled, False, actor=actor, reason=reason
        )
        log.warning("trading DISABLED by %s (%s)", actor, reason)
        await interaction.followup.send(
            embed=notice_embed(
                "Trading disabled",
                trading_change_summary(False, actor, reason)
                + "\nThe next confirmed request will be refused with "
                "`trading_disabled`. The position monitor keeps running (§58).",
            ),
            ephemeral=True,
        )

    @requires_authorization
    async def enable(self, interaction: discord.Interaction) -> None:
        """Shows the §57 details and requires a confirming press."""
        await interaction.response.defer(thinking=True, ephemeral=True)
        actor = str(interaction.user.id)
        view = EnableTradingView(self.context, actor)
        await interaction.followup.send(
            embed=notice_embed("Enable trading?", ENABLE_DETAILS),
            view=view,
            ephemeral=True,
        )


def register(tree: Any, context: BotContext) -> None:
    commands = TradingCommands(context)
    group = app_commands.Group(name="trading", description="The trading switch")

    @group.command(name="status", description="Is trading enabled?")
    async def status(interaction: discord.Interaction) -> None:
        await commands.status(interaction)

    @group.command(name="disable", description="Disable trading immediately")
    @app_commands.describe(reason="Why (recorded in the audit log)")
    async def disable(interaction: discord.Interaction, reason: str | None = None) -> None:
        await commands.disable(interaction, reason)

    @group.command(name="enable", description="Enable trading (requires confirmation)")
    async def enable(interaction: discord.Interaction) -> None:
        await commands.enable(interaction)

    tree.add_command(group)

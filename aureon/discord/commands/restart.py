"""Discord /restart — request a graceful full-stack supervisor restart."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import discord
from discord import app_commands

from aureon.discord.bot import requires_authorization
from aureon.discord.context import BotContext
from aureon.discord.embeds import notice_embed
from aureon.services.manual_restart import request_restart


class RestartCommands:
    def __init__(self, context: BotContext) -> None:
        self.context = context

    @requires_authorization
    async def restart(
        self,
        interaction: discord.Interaction,
        reason: str | None = None,
    ) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        repo_root = Path(__file__).resolve().parents[3]
        payload = await self.context.run(
            request_restart,
            repo_root,
            requested_by=str(interaction.user.id),
            reason=(reason or "Discord /restart"),
        )
        await interaction.followup.send(
            embed=notice_embed(
                "Aureon restart requested",
                (
                    "The supervisor will perform a graceful full-stack restart: "
                    "children are interrupted cooperatively, allowed to flush, then "
                    "main_aureon.py re-execs. New execution should be avoided until "
                    "the restarted status is LIVE again.\n\n"
                    f"Reason: {payload['reason']}"
                ),
            ),
            ephemeral=True,
        )


def register(tree: Any, context: BotContext) -> None:
    commands = RestartCommands(context)

    @tree.command(name="restart", description="Gracefully restart the full Aureon stack")
    @app_commands.describe(reason="Optional reason recorded in the restart notice")
    async def restart(
        interaction: discord.Interaction,
        reason: str | None = None,
    ) -> None:
        await commands.restart(interaction, reason)

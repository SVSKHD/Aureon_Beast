"""Discord /pull-history — latest main PR merges and completed restart time."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import discord

from aureon.discord.bot import requires_authorization
from aureon.discord.context import BotContext
from aureon.discord.embeds import notice_embed
from aureon.services.pull_history import (
    current_main_sha,
    read_restart_history,
    recent_pull_merges,
    restart_includes_merge,
)


class PullHistoryCommands:
    def __init__(self, context: BotContext) -> None:
        self.context = context

    @requires_authorization
    async def pull_history(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        repo_root = Path(__file__).resolve().parents[3]
        merges = await self.context.run(recent_pull_merges, repo_root, limit=5)
        restarts = await self.context.run(read_restart_history, repo_root)
        main_sha = await self.context.run(current_main_sha, repo_root)

        lines: list[str] = []
        if merges:
            lines.append("**Latest PR merges to main**")
            for index, merge in enumerate(merges, 1):
                lines.append(
                    f"{index}. PR #{merge.number} · `{merge.sha[:12]}` · "
                    f"{discord.utils.format_dt(merge.merged_at, style='R')}"
                )
        else:
            lines.append("No local PR merge history was found on main.")

        lines.append("")
        if restarts:
            latest_restart = restarts[0]
            lines.append("**Last completed Aureon restart**")
            lines.append(
                f"{discord.utils.format_dt(latest_restart.restarted_at, style='F')} "
                f"({discord.utils.format_dt(latest_restart.restarted_at, style='R')})"
            )
            lines.append(f"Reason: {latest_restart.reason}")
            if latest_restart.new_sha:
                lines.append(f"Running restart SHA: `{latest_restart.new_sha[:12]}`")
            if merges:
                included = await self.context.run(
                    restart_includes_merge,
                    repo_root,
                    merges[0].sha,
                    latest_restart,
                )
                if included is True:
                    lines.append(
                        f"Latest merged PR #{merges[0].number}: "
                        "included in that restart ✅"
                    )
                elif included is False:
                    lines.append(
                        f"Latest merged PR #{merges[0].number}: merged after that restart ⏳"
                    )
                else:
                    lines.append("Latest merged PR deployment status: unknown")
        else:
            lines.append("**Last completed Aureon restart**")
            lines.append("No completed restart has been recorded yet.")

        if main_sha:
            lines.append("")
            lines.append(f"Local main HEAD: `{main_sha[:12]}`")

        await interaction.followup.send(
            embed=notice_embed("Aureon pull history", "\n".join(lines)),
            ephemeral=True,
        )


def register(tree: Any, context: BotContext) -> None:
    commands = PullHistoryCommands(context)

    @tree.command(
        name="pull-history",
        description="Show recent PR merges to main and Aureon's last completed restart",
    )
    async def pull_history(interaction: discord.Interaction) -> None:
        await commands.pull_history(interaction)

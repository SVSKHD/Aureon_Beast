"""``/status`` (§59, §61-§63).

Reads Firestore and nothing else. Freshness is computed from ``updated_at`` at read time,
never from a stored flag -- a stored "live" would keep claiming liveness at exactly the
moment its writer died.
"""

from __future__ import annotations

import logging
from typing import Any

import discord

from aureon.discord.bot import requires_authorization
from aureon.discord.context import BotContext
from aureon.discord.embeds import notice_embed, status_embed
from aureon.discord.service import build_status
from aureon.models.enums import TradeRequestStatus

log = logging.getLogger(__name__)


class StatusCommands:
    def __init__(self, context: BotContext) -> None:
        self.context = context

    @requires_authorization
    async def status(self, interaction: discord.Interaction) -> None:
        # Deferred first: several Firestore reads follow and three seconds is not much.
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            screen = await self._build()
        except Exception:  # noqa: BLE001 - a status command must not die silently
            log.exception("/status failed")
            await interaction.followup.send(
                embed=notice_embed(
                    "Status unavailable",
                    "Could not read system state from Firestore. The services may still be "
                    "running — this command is the thing that failed.",
                    bad=True,
                ),
                ephemeral=True,
            )
            return
        await interaction.followup.send(embed=status_embed(screen), ephemeral=True)

    async def _build(self):
        context = self.context
        state = await context.run(context.system_state.read)
        heartbeats = await context.run(context.heartbeats.read_all)
        settings = await context.execution_settings()
        open_trades = await context.run(context.trades.open_trades)
        pending = await context.run(
            context.requests.list_by_status,
            [TradeRequestStatus.REQUESTED, TradeRequestStatus.CONFIRMED,
             TradeRequestStatus.PENDING],
        )
        # Phase 7 will supply reviews; until then build_status reports "no completed
        # review yet" rather than an empty panel.
        return build_status(
            system_state=state,
            heartbeats=heartbeats,
            settings=settings,
            open_trades=len(open_trades),
            pending_requests=len(pending),
            latest_review=None,
        )


def register(tree: Any, context: BotContext) -> None:
    commands = StatusCommands(context)

    @tree.command(name="status", description="Aureon system status")
    async def status(interaction: discord.Interaction) -> None:
        await commands.status(interaction)

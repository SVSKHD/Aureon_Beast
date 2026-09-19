"""``/cancel-order`` and ``/close-trade`` (§46, §47).

Both write a ``control_requests`` document and stop. Discord does not call the broker, and
it does **not** report the outcome from its own return value: the executor performs the
action under a claim/lease, and the monitor records what actually happened.

That indirection is the whole point. A cancel can lose a race to a fill, and a close can
lose one to a stop-loss. Reporting "cancelled" optimistically is how a human comes to
believe an order is gone when it is actually a live position.
"""

from __future__ import annotations

import logging
from typing import Any

import discord
from discord import app_commands

from aureon.discord.bot import requires_authorization
from aureon.discord.context import BotContext
from aureon.discord.embeds import notice_embed
from aureon.discord.service import build_control_request
from aureon.models.enums import ControlRequestKind

log = logging.getLogger(__name__)

ACKNOWLEDGEMENT = (
    "Requested. Aureon will perform this at the broker and report the **reconciled** "
    "result — not an optimistic one. Watch `/status`, or the trade will update itself.\n"
    "If it fails, the usual reason is that the market got there first: an order that "
    "filled cannot be cancelled, and a position that closed cannot be closed again."
)


class ControlCommands:
    def __init__(self, context: BotContext) -> None:
        self.context = context

    @requires_authorization
    async def cancel_order(self, interaction: discord.Interaction, order_ticket: str) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        await self._submit(
            interaction, ControlRequestKind.CANCEL, order_ticket, volume=None
        )

    @requires_authorization
    async def close_trade(
        self, interaction: discord.Interaction, position_id: str, volume: float | None = None
    ) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        if volume is not None and volume <= 0:
            await interaction.followup.send(
                embed=notice_embed(
                    "Cannot close", "volume must be greater than zero", bad=True
                ),
                ephemeral=True,
            )
            return
        await self._submit(
            interaction, ControlRequestKind.CLOSE, position_id, volume=volume
        )

    async def _submit(
        self,
        interaction: discord.Interaction,
        kind: ControlRequestKind,
        target: str,
        *,
        volume: float | None,
    ) -> None:
        if not str(target).isdigit():
            await interaction.followup.send(
                embed=notice_embed(
                    "Cannot proceed",
                    f"`{target}` is not a broker ticket or position id",
                    bad=True,
                ),
                ephemeral=True,
            )
            return

        request = build_control_request(
            kind, target, str(interaction.user.id), volume=volume
        )
        await self.context.run(self.context.controls.create, request)
        log.info("control request %s: %s %s", request.control_id, kind.value, target)

        await interaction.followup.send(
            embed=notice_embed(
                f"{kind.value.title()} requested", ACKNOWLEDGEMENT
            ),
            ephemeral=True,
        )


def register(tree: Any, context: BotContext) -> None:
    commands = ControlCommands(context)

    @tree.command(name="cancel-order", description="Cancel a resting pending order")
    @app_commands.describe(order_ticket="The broker order ticket")
    async def cancel_order(interaction: discord.Interaction, order_ticket: str) -> None:
        await commands.cancel_order(interaction, order_ticket)

    @tree.command(name="close-trade", description="Close an open position")
    @app_commands.describe(
        position_id="The MT5 position id",
        volume="Volume to close; omit to close all of it",
    )
    async def close_trade(
        interaction: discord.Interaction, position_id: str, volume: float | None = None
    ) -> None:
        await commands.close_trade(interaction, position_id, volume)

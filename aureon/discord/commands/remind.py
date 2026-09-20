"""``/remind`` — price alerts a human asked for (9C, §71).

    /remind price symbol:XAUUSD level:3700 side:above note:"watching the range high"
    /remind list
    /remind cancel id:al-1a2b3c4d5e

Three things this command is **not**:

* **not a trade.** An alert is a message about a price. It has no volume, no side of a
  trade, and no path to the broker; the `[Execute]` button on the reminder opens the same
  `/execute` confirmation everything else does, with the lot still typed and CONFIRM still
  required.
* **not a computation.** Discord reads the observer's published quote to check which side of
  the market the level is on, and writes one ``{prefix}_alerts`` document. The **observer**
  fires it, because firing needs the quotes it already reads and Discord may not call the
  broker (CLAUDE.md).
* **not shared.** An alert belongs to the user who armed it: only they are notified, and only
  they can cancel it. The repository enforces that too, so it holds however the cancel
  arrives (§71).

Every rule lives in ``service.plan_price_alert`` and ``service.render_alert_list``, tested
without a Discord interaction. What is left here is transport.
"""

from __future__ import annotations

import logging
from typing import Any

import discord
from discord import app_commands

from aureon.discord.bot import requires_authorization
from aureon.discord.context import BotContext
from aureon.discord.embeds import notice_embed
from aureon.discord.service import plan_price_alert, quote_of, render_alert_list
from aureon.models.alerts import ALERT_SIDES
from aureon.storage.alert_repository import AlertClaimRejected, AlertRejected

log = logging.getLogger(__name__)

ARMED_MESSAGE = (
    "Aureon will tell you **once**, when a quote it reads crosses this level. It expires in "
    "24 hours if it does not. Nothing is placed and nothing is sized — the reminder carries "
    "an Execute button, and that still asks for a lot and still needs CONFIRM."
)


class RemindCommands:
    def __init__(self, context: BotContext) -> None:
        self.context = context

    @requires_authorization
    async def price(
        self,
        interaction: discord.Interaction,
        symbol: str,
        level: float,
        side: str,
        note: str | None = None,
    ) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        context = self.context
        actor = str(interaction.user.id)
        symbol = symbol.upper()

        if context.alerts is None:  # pragma: no cover - wired in build_context
            await self._fail(interaction, "alerts are not configured in this deployment")
            return

        state = await context.run(
            context.system_state.read_symbol, symbol, context.config.timeframes[0]
        )
        # By keyword: ``run`` passes kwargs through, and ``armed(symbol, user_id)`` reads
        # plausibly enough positionally that a slip would silently count everybody's alerts
        # against this user's cap.
        armed = await context.run(context.alerts.armed, user_id=actor)
        plan = plan_price_alert(
            symbol=symbol,
            level=level,
            side=side,
            requested_by=actor,
            quote=quote_of(state, symbol),
            observed_symbols=context.config.symbols,
            armed_count=len(armed),
            note=note,
        )
        if not plan.ok or plan.alert is None:
            await self._fail(interaction, plan.message or "cannot arm this alert")
            return

        try:
            stored = await context.run(context.alerts.arm, plan.alert)
        except AlertRejected as exc:
            # The repository's own cap, which holds however the alert was created.
            await self._fail(interaction, str(exc))
            return

        await interaction.followup.send(
            embed=notice_embed(
                f"Armed · {stored.symbol} {stored.side} {stored.level:g}",
                f"`{stored.alert_id}` — {plan.note}\n\n{ARMED_MESSAGE}",
            ),
            ephemeral=True,
        )

    @requires_authorization
    async def list_alerts(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        context = self.context
        if context.alerts is None:  # pragma: no cover - wired in build_context
            await self._fail(interaction, "alerts are not configured in this deployment")
            return
        mine = await context.run(context.alerts.for_user, str(interaction.user.id))
        await interaction.followup.send(
            embed=notice_embed("Your alerts", render_alert_list(mine)),
            ephemeral=True,
        )

    @requires_authorization
    async def cancel(self, interaction: discord.Interaction, alert_id: str) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        context = self.context
        if context.alerts is None:  # pragma: no cover - wired in build_context
            await self._fail(interaction, "alerts are not configured in this deployment")
            return
        try:
            cancelled = await context.run(
                context.alerts.cancel, alert_id, actor=str(interaction.user.id)
            )
        except (AlertRejected, AlertClaimRejected) as exc:
            await self._fail(interaction, str(exc))
            return
        await interaction.followup.send(
            embed=notice_embed(
                "Cancelled",
                f"`{cancelled.alert_id}` {cancelled.symbol} {cancelled.side} "
                f"{cancelled.level:g} will not fire.",
            ),
            ephemeral=True,
        )

    async def _fail(self, interaction: discord.Interaction, message: str) -> None:
        await interaction.followup.send(
            embed=notice_embed("Cannot set this reminder", message, bad=True),
            ephemeral=True,
        )


def register(tree: Any, context: BotContext) -> None:
    commands = RemindCommands(context)
    group = app_commands.Group(
        name="remind", description="Price alerts (research only, never a trade)"
    )

    @group.command(name="price", description="Tell me when a symbol crosses a level")
    @app_commands.describe(
        symbol="Which symbol",
        level="The price to watch",
        side="above or below — which way price must cross it",
        note="Your own words, echoed back when it fires",
    )
    @app_commands.choices(
        symbol=[
            app_commands.Choice(name=name, value=name) for name in context.config.symbols
        ],
        side=[app_commands.Choice(name=name, value=name) for name in ALERT_SIDES],
    )
    async def price(
        interaction: discord.Interaction,
        symbol: str,
        level: float,
        side: str,
        note: str | None = None,
    ) -> None:
        await commands.price(interaction, symbol, level, side, note)

    @group.command(name="list", description="Your alerts, armed and answered")
    async def list_alerts(interaction: discord.Interaction) -> None:
        await commands.list_alerts(interaction)

    @group.command(name="cancel", description="Disarm one of your alerts")
    @app_commands.describe(id="The alert id from /remind list")
    async def cancel(interaction: discord.Interaction, id: str) -> None:  # noqa: A002
        await commands.cancel(interaction, id)

    tree.add_command(group)

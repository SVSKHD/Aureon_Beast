"""``/status`` (§59, §61-§63).

Reads Firestore and nothing else. Freshness is computed from ``updated_at`` at read time,
never from a stored flag -- a stored "live" would keep claiming liveness at exactly the
moment its writer died.

``symbol:`` narrows every number on the screen to one instrument (9A). Not a filter applied
to a global picture: it reads **that symbol's own state document**, so the freshness shown is
that symbol's, and the open-trade and pending-request counts are its own. A screen that
narrowed the panels but kept both symbols' counts would be the worst of the two, because the
count is the number a trader reads to decide whether they are exposed.
"""

from __future__ import annotations

import logging
from typing import Any

import discord
from discord import app_commands

from aureon.discord.bot import requires_authorization
from aureon.discord.context import BotContext
from aureon.discord.embeds import notice_embed, status_embed
from aureon.discord.service import build_status, for_symbol, unobserved_symbol_notice
from aureon.models.enums import TradeRequestStatus

log = logging.getLogger(__name__)


class StatusCommands:
    def __init__(self, context: BotContext) -> None:
        self.context = context

    @requires_authorization
    async def status(
        self, interaction: discord.Interaction, symbol: str | None = None
    ) -> None:
        # Deferred first: several Firestore reads follow and three seconds is not much.
        await interaction.response.defer(thinking=True, ephemeral=True)
        if symbol is not None:
            symbol = symbol.upper()
            unobserved = unobserved_symbol_notice(symbol, self.context.config.symbols)
            if unobserved:
                await interaction.followup.send(
                    embed=notice_embed("No such symbol here", unobserved, bad=True),
                    ephemeral=True,
                )
                return
        try:
            screen = await self._build(symbol)
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

    async def _build(self, symbol: str | None = None):
        context = self.context
        if symbol is None:
            state = await context.run(context.system_state.read)
        else:
            # One document, one read -- the reason the state was split per symbol (9A).
            state = await context.run(
                context.system_state.read_symbol, symbol, context.config.timeframes[0]
            )
        heartbeats = await context.run(context.heartbeats.read_all)
        settings = await context.execution_settings()
        open_trades = await context.run(context.trades.open_trades)
        pending = await context.run(
            context.requests.list_by_status,
            [TradeRequestStatus.REQUESTED, TradeRequestStatus.CONFIRMED,
             TradeRequestStatus.PENDING],
        )
        # §61-§63: on a closed market show the latest completed review. The weekly one
        # is preferred over the daily -- it is the wider picture, and on a weekend the most
        # recent daily covers Friday alone. Reviews are per symbol (9A), so a scoped screen
        # reads that symbol's and an unscoped one reads each observed symbol's: there is no
        # longer a single review that covers the deployment, and inventing one by taking
        # whichever sorted last would be a figure about nothing.
        latest = None
        reviews: dict[str, object] = {}
        if symbol is None:
            for name in context.config.symbols:
                found = await context.run(context.reviews.latest_for, name)
                if found is not None:
                    reviews[name] = found
            if len(context.config.symbols) == 1:
                latest = reviews.get(context.config.symbols[0])
                reviews = {}
        else:
            latest = await context.run(context.reviews.latest_for, symbol)
        return build_status(
            system_state=state,
            heartbeats=heartbeats,
            settings=settings,
            open_trades=len(for_symbol(open_trades, symbol)),
            pending_requests=len(for_symbol(pending, symbol)),
            latest_review=latest,
            latest_reviews=reviews,
            symbol=symbol,
        )


def register(tree: Any, context: BotContext) -> None:
    commands = StatusCommands(context)

    @tree.command(name="status", description="Aureon system status")
    @app_commands.describe(symbol="Narrow every number to one symbol")
    @app_commands.choices(
        symbol=[
            app_commands.Choice(name=name, value=name) for name in context.config.symbols
        ]
    )
    async def status(
        interaction: discord.Interaction, symbol: str | None = None
    ) -> None:
        await commands.status(interaction, symbol)

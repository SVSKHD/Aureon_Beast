"""``/execute`` — the market-order shortcut (9A, §37–§42).

``/execute-trade`` is a wizard: order type, stops, execution mode, guided detection
linking. It is the right shape for a pending order placed deliberately. It is the wrong
shape for the thing a trader does most often, which is a market order on a symbol they
are already watching — five options deep for one decision.

So: ``/execute symbol side lot [detection]``, one embed, CONFIRM.

## What it does not shorten

Everything that makes the confirmation meaningful:

* **the lot is typed.** There is no default and no "same as last time". A default lot is
  the one field where a wrong guess costs money, and a shortcut that supplies it is a
  shortcut to the wrong size.
* **CONFIRM is still required**, through the same transactional REQUESTED → CONFIRMED
  path as the wizard: only the requester may press it, a stale quote re-prompts with
  current numbers rather than refusing, the confirmation expires, and the transition is
  audited.
* **the broker is never called here.** This command reads Firestore — the published
  symbol spec, the observer's quote, the detection if one is named — and writes one
  ``trade_requests`` document. A boundary test forbids Discord from importing the
  execution package at all, and a behavioural test asserts zero broker calls before
  CONFIRM.
* **the filling mode is named**, not defaulted. FOK when the symbol supports it; IOC with
  an explicit note about partial fills when it does not; a refusal when neither is
  available (§39).

## The detection link is by id, not by guesswork

``/execute-trade`` offers the most recent linkable detection. Here the id is typed or
comes from a notification's button (9C), which makes the link an explicit statement by a
human rather than a proximity guess — and §50 keeps inferred links out of trade requests
entirely. A detection for a different symbol is refused rather than attached, because a
link that misdescribes what was acted on poisons every review built on it.

## Why this module is short

Every rule above is in ``service.plan_market_order``, tested without a Discord
interaction. What is left here is transport: defer, read Firestore, send one embed.
"""

from __future__ import annotations

import logging
from typing import Any

import discord
from discord import app_commands

from aureon.discord.bot import requires_authorization
from aureon.discord.context import BotContext
from aureon.discord.embeds import confirmation_embed, notice_embed
from aureon.discord.service import (
    MARKET_SIDES,
    attach_assessment,
    attach_risk_agent,
    build_confirmation,
    market_state_of,
    plan_market_order,
    quote_of,
)
from aureon.discord.views import ConfirmTradeView

log = logging.getLogger(__name__)


class ExecuteCommands:
    def __init__(self, context: BotContext) -> None:
        self.context = context

    @requires_authorization
    async def execute(
        self,
        interaction: discord.Interaction,
        symbol: str,
        side: str,
        lot: str,
        detection: str | None = None,
    ) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        context = self.context
        symbol = symbol.upper()

        linked = None
        if detection:
            linked = await context.run(context.detections.get, detection)
            if linked is None:
                await self._fail(interaction, f"no detection `{detection}`")
                return

        settings = await context.execution_settings()
        spec = await context.run(context.symbols.get, symbol)

        # This symbol's own state document, in one read (9A). Read BEFORE planning, because
        # the plan now refuses a closed market and the published sleep phase is where that
        # answer lives (11B).
        state = await context.run(
            context.system_state.read_symbol, symbol, context.config.timeframes[0]
        )
        plan = plan_market_order(
            symbol=symbol,
            side=side,
            lot=lot,
            requested_by=str(interaction.user.id),
            settings=settings,
            info=spec,
            observed_symbols=context.config.symbols,
            detection=linked,
            system_state=state,
        )
        if not plan.ok or plan.draft is None:
            # Every refusal happens here, before a trade_requests document exists: a
            # rejected shortcut must leave nothing behind for the executor to find.
            await self._fail(interaction, plan.message or "cannot place this trade")
            return

        quote = quote_of(state, symbol)
        if quote is None:
            await self._fail(
                interaction,
                f"No published quote for {symbol}. The observer may be stopped — check "
                "`/status`. Aureon will not show you a price it cannot vouch for.",
            )
            return

        screen = build_confirmation(
            plan.draft,
            quote,
            spec,
            settings,
            market_state=market_state_of(state, symbol),
            detection=linked,
        )
        risk = await attach_risk_agent(context, screen, plan.draft, quote, state)
        if plan.filling_note:
            # A substitution or an unknown mode is stated on the screen, never implied by
            # its absence (§39).
            screen.warnings.append(plan.filling_note)
        await attach_assessment(context, screen, symbol)
        if risk.verdict.value == "veto":
            await interaction.followup.send(
                embed=confirmation_embed(screen),
                ephemeral=True,
            )
            return
        request = await context.run(context.requests.create, plan.draft.to_request())
        await interaction.followup.send(
            embed=confirmation_embed(screen),
            view=ConfirmTradeView(context, request, plan.draft),
            ephemeral=True,
        )

    async def _fail(self, interaction: discord.Interaction, message: str) -> None:
        await interaction.followup.send(
            embed=notice_embed("Cannot place this trade", message, bad=True),
            ephemeral=True,
        )


def register(tree: Any, context: BotContext) -> None:
    commands = ExecuteCommands(context)
    choices = [
        app_commands.Choice(name=symbol, value=symbol)
        for symbol in context.config.symbols
    ]

    @tree.command(name="execute", description="Market order (requires confirmation)")
    @app_commands.describe(
        symbol="Which symbol",
        side="buy or sell",
        lot="Lot size — no default, type it",
        detection="Optional detection_id to link explicitly",
    )
    @app_commands.choices(
        symbol=choices,
        side=[app_commands.Choice(name=name, value=name) for name in MARKET_SIDES],
    )
    async def execute(
        interaction: discord.Interaction,
        symbol: str,
        side: str,
        lot: str,
        detection: str | None = None,
    ) -> None:
        await commands.execute(interaction, symbol, side, lot, detection)

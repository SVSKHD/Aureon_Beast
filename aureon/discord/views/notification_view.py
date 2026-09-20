"""The two buttons under a detection embed and a fired reminder (9C).

    [Monitor]  → the §60 assessment for this detection (9D)
    [Execute]  → the /execute confirmation, prefilled with symbol, side and detection

## What [Execute] does NOT do

It does not place anything, and it does not shorten the confirmation. Pressing it opens a
**modal asking for the lot** — because a button that carried a size would make the most
consequential number in the system a thing somebody tapped rather than typed — and the lot
then goes through exactly the same ``plan_market_order`` and ``ConfirmTradeView`` as
``/execute`` itself. CONFIRM is still required, the quote is still checked, and the broker is
still never called by Discord.

A prefilled symbol and side are different in kind from a prefilled lot: they are what the
embed is *about*, and getting them from the detection removes a retyping error rather than a
decision. The lot is the decision.

## Why the detection id rides along

An execution opened from a detection embed carries that detection's id into the trade request
(§50), which makes the link an explicit statement by a human rather than a proximity guess in
a later review.
"""

from __future__ import annotations

import logging

import discord

from aureon.discord.context import BotContext
from aureon.discord.embeds import confirmation_embed, notice_embed
from aureon.discord.service import (
    attach_assessment,
    build_confirmation,
    market_state_of,
    plan_market_order,
    quote_of,
)
from aureon.discord.views.confirm_view import ConfirmTradeView

log = logging.getLogger(__name__)


class LotModal(discord.ui.Modal):
    """Asks for the one number a button may not supply (9C)."""

    def __init__(
        self,
        context: BotContext,
        *,
        symbol: str,
        side: str,
        detection_id: str | None,
    ) -> None:
        super().__init__(title=f"{side.upper()} {symbol}")
        self.context = context
        self.symbol = symbol
        self.side = side
        self.detection_id = detection_id
        self.lot = discord.ui.TextInput(
            label="Lot size",
            placeholder="e.g. 0.10 — no default, and no 'same as last time'",
            required=True,
            max_length=12,
        )
        self.add_item(self.lot)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        context = self.context
        settings = await context.execution_settings()
        spec = await context.run(context.symbols.get, self.symbol)

        linked = None
        if self.detection_id:
            linked = await context.run(context.detections.get, self.detection_id)

        plan = plan_market_order(
            symbol=self.symbol,
            side=self.side,
            lot=str(self.lot.value),
            requested_by=str(interaction.user.id),
            settings=settings,
            info=spec,
            observed_symbols=context.config.symbols,
            detection=linked,
        )
        if not plan.ok or plan.draft is None:
            await interaction.followup.send(
                embed=notice_embed(
                    "Cannot place this trade", plan.message or "refused", bad=True
                ),
                ephemeral=True,
            )
            return

        state = await context.run(
            context.system_state.read_symbol, self.symbol, context.config.timeframes[0]
        )
        quote = quote_of(state, self.symbol)
        if quote is None:
            await interaction.followup.send(
                embed=notice_embed(
                    "Cannot place this trade",
                    f"No published quote for {self.symbol}. The observer may be stopped — "
                    "check `/status`.",
                    bad=True,
                ),
                ephemeral=True,
            )
            return

        request = await context.run(context.requests.create, plan.draft.to_request())
        screen = build_confirmation(
            plan.draft,
            quote,
            spec,
            settings,
            market_state=market_state_of(state, self.symbol),
            detection=linked,
        )
        if plan.filling_note:
            screen.warnings.append(plan.filling_note)
        await attach_assessment(context, screen, self.symbol)
        await interaction.followup.send(
            embed=confirmation_embed(screen),
            view=ConfirmTradeView(context, request, plan.draft),
            ephemeral=True,
        )


class NotificationView(discord.ui.View):
    """``[Monitor]`` and ``[Execute]`` under an announcement (9C)."""

    def __init__(
        self,
        context: BotContext,
        *,
        symbol: str,
        detection_id: str | None = None,
        side: str | None = None,
        timeout: float | None = None,
    ) -> None:
        # No timeout by default: the embed stays in the channel, and a button that stops
        # working after a few minutes leaves a message that looks live and is not.
        super().__init__(timeout=timeout)
        self.context = context
        self.symbol = symbol
        self.detection_id = detection_id
        self.side = side
        if side is None:
            # A context-only detection has no side to prefill, and inventing one is exactly
            # the guess this whole design refuses to make.
            self.remove_item(self.execute)

    @discord.ui.button(label="Monitor", style=discord.ButtonStyle.secondary)
    async def monitor(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        """Points at ``/monitor`` (9D), which is the assessment surface.

        Deliberately a pointer rather than a second implementation: two renderings of "how
        is this detection doing" would drift, and the one in a notification would be the
        one nobody updated.
        """
        target = (
            f"`/monitor detection:{self.detection_id}`"
            if self.detection_id
            else f"`/monitor symbol:{self.symbol}`"
        )
        await interaction.response.send_message(
            embed=notice_embed(
                "Monitor",
                f"Run {target} for the measured assessment. Every percentage there is a "
                "frequency from stored outcomes, never a forecast.",
            ),
            ephemeral=True,
        )

    @discord.ui.button(label="Execute", style=discord.ButtonStyle.primary)
    async def execute(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        """Opens the lot modal. Nothing is placed until CONFIRM, as everywhere else."""
        if not self.context.config.is_authorized(str(interaction.user.id)):
            await interaction.response.send_message(
                embed=notice_embed(
                    "Not authorized",
                    "This channel is readable by more people than may trade (§71).",
                    bad=True,
                ),
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(
            LotModal(
                self.context,
                symbol=self.symbol,
                side=self.side or "buy",
                detection_id=self.detection_id,
            )
        )

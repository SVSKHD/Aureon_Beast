"""The two buttons under a setup card (12, T-11).

    [Monitor]  → points at `/monitor`, the measured assessment surface (9D)
    [Execute]  → the same lot modal every other execution path uses

## What [Execute] prefills, and what it does not

BULLISH prefills BUY and BEARISH prefills SELL, the same convenience the detection card offers.
NEUTRAL prefills nothing and the button is removed: a MOMENTUM_TRANSITION that confirmed without
resolving its direction has no side to offer, and inventing one is exactly the guess this design
refuses.

The mapping is worth naming plainly, because it is the one place this system draws an equivalence
between a description and an action: ``direction_context`` describes a structure and ``side`` is
something a human does. The equivalence is a deliberate choice, made because a prefilled side
removes a retyping error rather than a decision -- the LOT is the decision, and it is still typed
into a modal, still planned through ``plan_market_order``, still confirmed through
``ConfirmTradeView``, and the broker is still never called by Discord.

## No setup id rides into the trade request

A detection id on a trade request is an explicit statement that this human acted on that
observation (§50), and the review machinery counts it. A setup is not an observation of one
candle; linking one would put a claim in ``trades`` that the review's ``detection_id`` machinery
cannot interpret. The setup's own linked detection ids are on the card for a human to use.

"""

from __future__ import annotations

import logging

import discord

from aureon.discord.context import BotContext
from aureon.discord.embeds import notice_embed
from aureon.discord.views.notification_view import LotModal

log = logging.getLogger(__name__)


class SetupView(discord.ui.View):
    """``[Monitor]`` and ``[Execute]`` under a setup card (T-11)."""

    def __init__(
        self,
        context: BotContext,
        *,
        symbol: str,
        setup_id: str,
        side: str | None = None,
        timeout: float | None = None,
    ) -> None:
        # No timeout: the card is edited in place for as long as the setup lives, and a button
        # that stopped working would leave a message that looks live and is not.
        super().__init__(timeout=timeout)
        self.context = context
        self.symbol = symbol
        self.setup_id = setup_id
        self.side = side
        if side is None:
            # A NEUTRAL direction context has no side to prefill, and inventing one is exactly
            # the guess this whole design refuses to make.
            self.remove_item(self.execute)

    @discord.ui.button(label="Monitor", style=discord.ButtonStyle.secondary)
    async def monitor(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        """Points at ``/monitor`` and at ``/setup``, rather than rendering a second readout.

        Two renderings of "how is this doing" would drift, and the one inside a button is the one
        nobody updates.
        """
        await interaction.response.send_message(
            embed=notice_embed(
                "Monitor",
                f"`/setup id:{self.setup_id}` for this setup's current card, and "
                f"`/monitor symbol:{self.symbol}` for the measured assessment. Every "
                "percentage in either is a frequency from stored outcomes, never a forecast.",
            ),
            ephemeral=True,
        )

    @discord.ui.button(label="Execute", style=discord.ButtonStyle.primary)
    async def execute(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        """Opens the lot modal, prefilled with the symbol and side. Nothing is placed until
        CONFIRM -- the same ``plan_market_order`` and ``ConfirmTradeView`` as ``/execute``."""
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
                side=self.side,
                # Deliberately no detection id: see the module docstring.
                detection_id=None,
            )
        )


__all__ = ["LotModal", "SetupView"]

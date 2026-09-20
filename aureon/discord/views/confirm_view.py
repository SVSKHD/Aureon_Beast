"""The CONFIRM button (§27, §37, §40).

The single most consequential control in the system: pressing it authorises real money.
Three properties it must have, all enforced in ``service.check_confirm_press`` and merely
*rendered* here:

* **only the requester may press it.** Discord messages are visible to a channel, so
  another authorised user could otherwise confirm someone else's trade (§27).
* **a stale quote re-prompts rather than refuses.** The human still wants the trade; they
  need current numbers (§27).
* **a second press is harmless.** ``TradeRequestRepository.confirm`` is idempotent, and
  the view disables itself on success so the second press is unlikely in the first place.

The view never reports an outcome. It confirms, and the result arrives later from a
Firestore listener watching that request -- because what actually happened is the
executor's and the broker's account, not this button's.
"""

from __future__ import annotations

import logging

import discord

from aureon.discord.context import BotContext
from aureon.discord.embeds import confirmation_embed, notice_embed
from aureon.discord.service import (
    DraftRequest,
    build_confirmation,
    check_confirm_press,
    quote_of,
    trading_change_summary,
)
from aureon.models.enums import MarketState
from aureon.models.trade import TradeRequest

log = logging.getLogger(__name__)


class ConfirmTradeView(discord.ui.View):
    """CONFIRM / CANCEL for one pending trade request."""

    def __init__(
        self,
        context: BotContext,
        request: TradeRequest,
        draft: DraftRequest,
        *,
        timeout: float | None = None,
    ) -> None:
        # The view's timeout mirrors the confirmation TTL, so the buttons stop working at
        # the same moment the backend would refuse them anyway.
        super().__init__(
            timeout=timeout or context.config.confirmation_ttl_seconds
        )
        self.context = context
        self.request = request
        self.draft = draft
        self.confirmed = False

    async def _reject(self, interaction: discord.Interaction, message: str) -> None:
        await interaction.response.send_message(
            embed=notice_embed("Not confirmed", message, bad=True), ephemeral=True
        )

    @discord.ui.button(label="CONFIRM", style=discord.ButtonStyle.success)
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        settings = await self.context.execution_settings()
        # A FRESH quote, not the one on the screen: the point is to check whether the
        # price the human is looking at still exists.
        quote = await self.context.run(_latest_quote, self.context, self.request.symbol)

        gate = check_confirm_press(
            self.request, str(interaction.user.id), quote, settings
        )
        if not gate.ok:
            if gate.needs_refresh and quote is not None:
                await self._re_prompt(interaction, quote, settings)
                return
            await self._reject(interaction, gate.reason or "not allowed")
            return

        confirmed = await self.context.run(
            self.context.requests.confirm,
            self.request.request_id,
            str(interaction.user.id),
            quote,
            confirmation_ttl_seconds=settings.confirmation_ttl_seconds,
        )
        self.confirmed = True
        self.request = confirmed

        # Disable everything: the authorisation has been given and cannot be given twice.
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        self.stop()

        await interaction.response.edit_message(
            embed=notice_embed(
                "Confirmed",
                f"Request `{confirmed.request_id}` is confirmed and queued for execution. "
                "The result will appear here.",
            ),
            view=self,
        )

    async def _re_prompt(
        self, interaction: discord.Interaction, quote, settings
    ) -> None:
        """Show the screen again with current numbers (§27)."""
        screen = build_confirmation(
            self.draft,
            quote,
            await self.context.run(self.context.symbols.get, self.draft.symbol),
            settings,
            market_state=MarketState.UNKNOWN,
        )
        screen.warnings.insert(
            0, "The previous quote went stale. These are current prices — confirm again."
        )
        await interaction.response.edit_message(embed=confirmation_embed(screen), view=self)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if str(interaction.user.id) != str(self.request.requested_by):
            await self._reject(interaction, "only the requester may cancel this request")
            return
        from aureon.models.enums import TradeRequestStatus

        await self.context.run(
            self.context.requests.resolve,
            self.request.request_id,
            "discord",
            TradeRequestStatus.CANCELLED,
            failure_message="cancelled in Discord before confirmation",
            reconciliation=True,
        )
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        self.stop()
        await interaction.response.edit_message(
            embed=notice_embed("Cancelled", "The request was cancelled and never sent."),
            view=self,
        )


class EnableTradingView(discord.ui.View):
    """The confirm step for ``/trading enable`` (§57).

    Enabling needs a second press; **disabling does not**. The asymmetry is deliberate:
    disabling is the safe direction and must be instant in an incident, while enabling arms
    real money and deserves a pause.
    """

    def __init__(self, context: BotContext, actor: str, *, timeout: float = 60.0) -> None:
        super().__init__(timeout=timeout)
        self.context = context
        self.actor = actor
        self.enabled = False

    @discord.ui.button(label="Enable trading", style=discord.ButtonStyle.danger)
    async def enable(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if str(interaction.user.id) != str(self.actor):
            await interaction.response.send_message(
                embed=notice_embed(
                    "Not enabled", "only the requesting user may confirm this", bad=True
                ),
                ephemeral=True,
            )
            return
        await self.context.run(
            self.context.settings.set_trading_enabled, True, actor=self.actor
        )
        self.enabled = True
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        self.stop()
        await interaction.response.edit_message(
            embed=notice_embed("Trading enabled", trading_change_summary(True, self.actor)),
            view=self,
        )


def _latest_quote(context: BotContext, symbol: str):
    """The freshest quote Discord can honestly offer, from Firestore only.

    Discord never calls the broker (CLAUDE.md), so this is the ``last_quote`` the observer
    publishes into ``system_state`` -- one current snapshot per symbol, not a tick stream
    (decision 80). Its ``captured_at`` is what the staleness check measures, so a stopped
    observer correctly makes every confirmation re-prompt rather than proceeding on a price
    nobody can vouch for.

    Returns ``None`` when no quote has been published, which ``check_confirm_press`` treats
    as "cannot confirm, refresh" rather than as permission to proceed.
    """
    return quote_of(context.system_state.read(), symbol)

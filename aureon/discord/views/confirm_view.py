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

The view never INVENTS an outcome. After confirmation it polls the stored TradeRequest,
which is resolved only by the executor/reconciliation path, and edits the same Discord
message with the stored success/failure result. Discord still never calls the broker.
"""

from __future__ import annotations

import asyncio
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
from aureon.models.enums import MarketState, TradeRequestStatus
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
                "Waiting for the executor result…",
            ),
            view=self,
        )
        message = interaction.message
        if message is not None:
            asyncio.create_task(
                self._watch_execution_result(message),
                name=f"aureon-discord-result-{confirmed.request_id}",
            )

    async def _watch_execution_result(
        self,
        message: discord.Message,
        *,
        timeout_seconds: float = 120.0,
        poll_seconds: float = 0.5,
    ) -> None:
        """Edit the confirmation message when the executor resolves this request.

        The repository is the only source here. Discord never calls MT5 and never treats the
        confirm callback itself as execution success.
        """
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        terminal_or_accepted = {
            TradeRequestStatus.FILLED,
            TradeRequestStatus.PARTIALLY_FILLED,
            TradeRequestStatus.PENDING,
            TradeRequestStatus.FAILED,
            TradeRequestStatus.FAILED_STALE,
            TradeRequestStatus.FAILED_RECONCILIATION,
            TradeRequestStatus.CANCELLED,
            TradeRequestStatus.EXPIRED,
        }
        while asyncio.get_running_loop().time() < deadline:
            try:
                current = await self.context.run(
                    self.context.requests.get,
                    self.request.request_id,
                )
            except Exception:  # noqa: BLE001 - reporting must not affect execution
                log.exception(
                    "could not read execution result for %s",
                    self.request.request_id,
                )
                return
            if current is None:
                return
            if current.status in terminal_or_accepted:
                try:
                    await message.edit(
                        embed=_execution_result_embed(current),
                        view=self,
                    )
                except Exception:  # noqa: BLE001 - broker result is already persisted
                    log.exception(
                        "could not edit Discord execution result for %s",
                        current.request_id,
                    )
                return
            await asyncio.sleep(poll_seconds)

        try:
            await message.edit(
                embed=notice_embed(
                    "Execution still pending",
                    (
                        f"Request `{self.request.request_id}` has not reached a broker result "
                        f"within {timeout_seconds:.0f}s. Use `/status` to inspect the executor; "
                        "the stored request remains authoritative."
                    ),
                ),
                view=self,
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "could not post pending execution status for %s",
                self.request.request_id,
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


def _execution_result_embed(request: TradeRequest) -> discord.Embed:
    """Render the executor-owned result stored on a TradeRequest."""
    status = request.status
    request_id = request.request_id

    if status is TradeRequestStatus.FILLED:
        details = [
            f"Request `{request_id}` was **FILLED** by the broker.",
            f"Filled volume: `{request.filled_volume:g}`"
            if request.filled_volume is not None
            else "Filled volume: —",
            f"Fill price: `{request.fill_price:g}`"
            if request.fill_price is not None
            else "Fill price: —",
        ]
        if request.position_id is not None:
            details.append(f"Position: `{request.position_id}`")
        if request.order_ticket is not None:
            details.append(f"Order ticket: `{request.order_ticket}`")
        return notice_embed("✅ Execution succeeded", "\n".join(details))

    if status is TradeRequestStatus.PENDING:
        details = [
            f"Request `{request_id}` was accepted as a **PENDING** broker order.",
        ]
        if request.order_ticket is not None:
            details.append(f"Order ticket: `{request.order_ticket}`")
        return notice_embed("✅ Pending order accepted", "\n".join(details))

    if status is TradeRequestStatus.PARTIALLY_FILLED:
        details = [
            f"Request `{request_id}` was **PARTIALLY FILLED**.",
            f"Filled volume: `{request.filled_volume:g}`"
            if request.filled_volume is not None
            else "Filled volume: —",
        ]
        if request.fill_price is not None:
            details.append(f"Fill price: `{request.fill_price:g}`")
        return notice_embed("🟡 Partial execution", "\n".join(details))

    failure_code = (
        request.failure_code.value
        if getattr(request.failure_code, "value", None) is not None
        else request.failure_code
    )
    reason = request.failure_message or "No broker/executor failure message was stored."
    details = [
        f"Request `{request_id}` ended as **{status.value.upper()}**.",
        f"Failure code: `{failure_code}`" if failure_code else "Failure code: —",
        f"Reason: {reason}",
    ]
    return notice_embed("❌ Execution failed", "\n".join(details), bad=True)


class EnableTradingView(discord.ui.View):
    """The confirm step for ``/trading enable`` (§57).

    Enabling needs a second press; **disabling does not**. The asymmetry is deliberate:
    disabling is the safe direction and must be instant in an incident, while enabling arms
    real money and deserves a pause.
    """

    def __init__(
        self,
        context: BotContext,
        actor: str,
        *,
        timeout: float = 60.0,
        live: bool = False,
    ) -> None:
        super().__init__(timeout=timeout)
        self.context = context
        self.actor = actor
        self.enabled = False
        #: 11A F-3. Only relabels the button. The DECISION was made by
        #: ``plan_trading_enable`` before this view was built -- a live account without the
        #: environment flag never gets a view at all -- so this cannot become the place
        #: where the gate is enforced, or forgotten.
        self.live = live
        if live:
            self.enable.label = "Enable on LIVE account"

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

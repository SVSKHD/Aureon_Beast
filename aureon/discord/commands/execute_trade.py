"""``/execute-trade`` (§37-§42).

The wizard: symbol → direction/order type → lot → stops → execution mode → optional
detection link → confirmation screen → CONFIRM.

Everything that could refuse the trade is surfaced **before** the confirmation screen
wherever possible: an illegal lot, an unsupported filling mode, a closed market, a spread
over the limit. A rejection after CONFIRM is opaque and alarming; the same information a
moment earlier is just useful.

The request is created as ``REQUESTED`` here and confirmed only by the button. Discord
never confirms on the human's behalf.
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
    DraftRequest,
    attach_assessment,
    attach_risk_agent,
    build_confirmation,
    linkable_detections,
    market_state_of,
    quote_of,
    unobserved_symbol_notice,
    unsupported_mode_notice,
    validate_lot,
)
from aureon.discord.views import ConfirmTradeView
from aureon.models.enums import FillingMode, OrderType

log = logging.getLogger(__name__)


class ExecuteTradeCommands:
    def __init__(self, context: BotContext) -> None:
        self.context = context

    @requires_authorization
    async def execute_trade(
        self,
        interaction: discord.Interaction,
        symbol: str,
        order_type: str,
        lot: str,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        entry: float | None = None,
        execution_mode: str | None = None,
        link_detection: bool = False,
    ) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        context = self.context
        actor = str(interaction.user.id)
        symbol = symbol.upper()

        # 9A: the same gate as /execute. Without an observer for this symbol there is no
        # published quote and no spec, so the wizard could only show blanks.
        unobserved = unobserved_symbol_notice(symbol, context.config.symbols)
        if unobserved:
            await self._fail(interaction, unobserved)
            return

        try:
            kind = OrderType(order_type)
        except ValueError:
            await self._fail(interaction, f"`{order_type}` is not a known order type")
            return

        if kind.is_pending and entry is None:
            await self._fail(
                interaction,
                f"{kind.value} needs an `entry` price — a pending order has no market entry",
            )
            return

        settings = await context.execution_settings()
        spec = await context.run(context.symbols.get, symbol)

        # §42: validated before the confirmation screen, so the human can still fix it.
        check = validate_lot(lot, spec, settings)
        if not check.ok:
            await self._fail(interaction, check.message or "invalid lot size")
            return

        mode: FillingMode | None = None
        if execution_mode:
            try:
                mode = FillingMode(execution_mode)
            except ValueError:
                await self._fail(interaction, f"`{execution_mode}` is not an execution mode")
                return
            # §39: say so plainly rather than silently omitting the option.
            notice = unsupported_mode_notice(spec, mode)
            if notice:
                await self._fail(interaction, notice)
                return

        detection = None
        if link_detection:
            detection = await self._pick_detection(symbol)

        draft = DraftRequest(
            symbol=symbol,
            order_type=kind,
            volume=check.normalized or float(lot),
            requested_by=actor,
            price=entry,
            sl=stop_loss,
            tp=take_profit,
            filling_mode=mode,
            deviation_points=settings.max_deviation_points,
            detection_id=detection.detection_id if detection else None,
        )

        state = await context.run(context.system_state.read)
        quote = quote_of(state, symbol)
        if quote is None:
            await self._fail(
                interaction,
                f"No published quote for {symbol}. The observer may be stopped — check "
                "`/status`. Aureon will not show you a price it cannot vouch for.",
            )
            return

        market_state = market_state_of(state, symbol)
        screen = build_confirmation(
            draft, quote, spec, settings, market_state=market_state, detection=detection
        )
        risk = await attach_risk_agent(context, screen, draft, quote, state)
        await attach_assessment(context, screen, draft.symbol)
        if risk.verdict.value == "veto":
            await interaction.followup.send(
                embed=confirmation_embed(screen), ephemeral=True
            )
            return
        request = await context.run(context.requests.create, draft.to_request())
        view = ConfirmTradeView(context, request, draft)
        await interaction.followup.send(
            embed=confirmation_embed(screen), view=view, ephemeral=True
        )

    async def _pick_detection(self, symbol: str):
        """The most recent linkable detection, if any (§37).

        The window matters: a detection from hours ago is not what someone is reacting to,
        and offering it would produce a link that misleads every review built on it.
        """
        context = self.context
        recent = await context.run(
            context.detections.recent_for_symbol, symbol.upper(), None, 25
        )
        candidates = linkable_detections(
            recent,
            symbol=symbol,
            window_minutes=context.config.link_window_minutes,
        )
        return candidates[0] if candidates else None

    async def _fail(self, interaction: discord.Interaction, message: str) -> None:
        await interaction.followup.send(
            embed=notice_embed("Cannot place this trade", message, bad=True), ephemeral=True
        )


def register(tree: Any, context: BotContext) -> None:
    commands = ExecuteTradeCommands(context)

    @tree.command(name="execute-trade", description="Place a trade (requires confirmation)")
    @app_commands.choices(
        symbol=[
            app_commands.Choice(name=name, value=name) for name in context.config.symbols
        ]
    )
    @app_commands.describe(
        symbol="Which symbol",
        order_type="market_buy, market_sell, buy_stop, sell_stop, buy_limit, sell_limit",
        lot="Lot size, e.g. 0.10",
        stop_loss="Stop loss price",
        take_profit="Take profit price",
        entry="Entry price (pending orders only)",
        execution_mode="fok, ioc or return — only modes the symbol supports are accepted",
        link_detection="Link the most recent detection for this symbol",
    )
    async def execute_trade(
        interaction: discord.Interaction,
        symbol: str,
        order_type: str,
        lot: str,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        entry: float | None = None,
        execution_mode: str | None = None,
        link_detection: bool = False,
    ) -> None:
        await commands.execute_trade(
            interaction,
            symbol,
            order_type,
            lot,
            stop_loss,
            take_profit,
            entry,
            execution_mode,
            link_detection,
        )

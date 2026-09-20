"""``/cancel-order``, ``/close-trade`` and ``/close`` (§46, §47).

Both write a ``control_requests`` document and stop. Discord does not call the broker, and
it does **not** report the outcome from its own return value: the executor performs the
action under a claim/lease, and the monitor records what actually happened.

That indirection is the whole point. A cancel can lose a race to a fill, and a close can
lose one to a stop-loss. Reporting "cancelled" optimistically is how a human comes to
believe an order is gone when it is actually a live position.

## ``symbol:`` is a cross-check, not a filter (9A)

A broker ticket is eight digits nobody can read. With two symbols on one account a mistyped
one points at a real order belonging to the other instrument, and the request succeeds --
against the wrong trade. Naming the symbol turns that typo into a refusal: Aureon looks the
target up in Firestore, refuses a mismatch, and refuses an unverifiable claim too, because a
guarantee that quietly lapses when the record is missing is not one. The raw ticket is still
available by omitting ``symbol:``, which is then plainly an unchecked action.

The named symbol also rides along on the ``control_requests`` document, so the executor
re-checks it against the live position or order before touching the broker. Only the symbol a
human actually asserted is sent: attaching one Aureon merely inferred would let a stale record
block a close, and a close is the risk-reducing direction.

## ``/close symbol:`` is the shortcut

One position on that symbol: close it, no ids to copy. None, or more than one: it says what it
found and stops. Choosing which of a human's open trades to end is not a default Aureon gets
to pick.
"""

from __future__ import annotations

import logging
from typing import Any

import discord
from discord import app_commands

from aureon.discord.bot import requires_authorization
from aureon.discord.context import BotContext
from aureon.discord.embeds import notice_embed
from aureon.discord.service import (
    build_control_request,
    check_target_symbol,
    resolve_close_target,
    unobserved_symbol_notice,
)
from aureon.models.enums import ControlRequestKind, TradeRequestStatus

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
    async def cancel_order(
        self,
        interaction: discord.Interaction,
        order_ticket: str,
        symbol: str | None = None,
    ) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        if not await self._symbol_is_observed(interaction, symbol):
            return
        await self._submit(
            interaction,
            ControlRequestKind.CANCEL,
            order_ticket,
            volume=None,
            symbol=symbol,
        )

    @requires_authorization
    async def close_trade(
        self,
        interaction: discord.Interaction,
        position_id: str,
        volume: float | None = None,
        symbol: str | None = None,
    ) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        if volume is not None and volume <= 0:
            await self._refuse(interaction, "Cannot close", "volume must be greater than zero")
            return
        if not await self._symbol_is_observed(interaction, symbol):
            return
        await self._submit(
            interaction,
            ControlRequestKind.CLOSE,
            position_id,
            volume=volume,
            symbol=symbol,
        )

    @requires_authorization
    async def close(
        self, interaction: discord.Interaction, symbol: str, volume: float | None = None
    ) -> None:
        """``/close symbol:`` -- the one open position on that symbol (9A)."""
        await interaction.response.defer(thinking=True, ephemeral=True)
        if volume is not None and volume <= 0:
            await self._refuse(interaction, "Cannot close", "volume must be greater than zero")
            return
        symbol = symbol.upper()
        if not await self._symbol_is_observed(interaction, symbol):
            return

        open_trades = await self.context.run(self.context.trades.open_trades)
        choice = resolve_close_target(open_trades, symbol)
        if not choice.ok or choice.position_id is None:
            await self._refuse(
                interaction, "Nothing closed", choice.message or "no position to close"
            )
            return
        await self._submit(
            interaction,
            ControlRequestKind.CLOSE,
            str(choice.position_id),
            volume=volume,
            symbol=symbol,
            verified_symbol=choice.symbol,
        )

    async def _symbol_is_observed(
        self, interaction: discord.Interaction, symbol: str | None
    ) -> bool:
        if symbol is None:
            return True
        unobserved = unobserved_symbol_notice(symbol, self.context.config.symbols)
        if unobserved:
            await self._refuse(interaction, "No such symbol here", unobserved)
            return False
        return True

    async def _target_symbol(self, kind: ControlRequestKind, target: str) -> str | None:
        """What Aureon's own records say this ticket or position id belongs to.

        Firestore only -- Discord never calls the broker (CLAUDE.md). A close is looked up
        by position id among the trades the monitor imported; a cancel by order ticket among
        the requests that placed one, which is the only record Discord has of a resting
        order's symbol.
        """
        context = self.context
        if kind is ControlRequestKind.CLOSE:
            trade = await context.run(context.trades.get_by_position, int(target))
            return trade.symbol if trade is not None else None
        requests = await context.run(
            context.requests.list_by_status,
            [TradeRequestStatus.PENDING, TradeRequestStatus.FILLED],
        )
        ticket = int(target)
        for request in requests:
            if request.order_ticket == ticket:
                return request.symbol
        return None

    async def _submit(
        self,
        interaction: discord.Interaction,
        kind: ControlRequestKind,
        target: str,
        *,
        volume: float | None,
        symbol: str | None = None,
        verified_symbol: str | None = None,
    ) -> None:
        if not str(target).isdigit():
            await self._refuse(
                interaction,
                "Cannot proceed",
                f"`{target}` is not a broker ticket or position id",
            )
            return

        if symbol is not None:
            # ``verified_symbol`` is set when the target was resolved BY symbol, as
            # ``/close`` does -- looking it up again would only re-read what we just read.
            found = verified_symbol
            if found is None:
                found = await self._target_symbol(kind, target)
            check = check_target_symbol(
                symbol=symbol, kind=kind, target=target, found_symbol=found
            )
            if not check.ok:
                await self._refuse(
                    interaction, "Nothing sent", check.message or "symbol mismatch"
                )
                return

        request = build_control_request(
            kind, target, str(interaction.user.id), symbol=symbol, volume=volume
        )
        await self.context.run(self.context.controls.create, request)
        log.info("control request %s: %s %s", request.control_id, kind.value, target)

        await interaction.followup.send(
            embed=notice_embed(
                f"{kind.value.title()} requested"
                + (f" — {symbol} `{target}`" if symbol else ""),
                ACKNOWLEDGEMENT,
            ),
            ephemeral=True,
        )

    async def _refuse(
        self, interaction: discord.Interaction, title: str, message: str
    ) -> None:
        await interaction.followup.send(
            embed=notice_embed(title, message, bad=True), ephemeral=True
        )


def register(tree: Any, context: BotContext) -> None:
    commands = ControlCommands(context)
    symbols = [
        app_commands.Choice(name=name, value=name) for name in context.config.symbols
    ]

    @tree.command(name="cancel-order", description="Cancel a resting pending order")
    @app_commands.describe(
        order_ticket="The broker order ticket",
        symbol="Optional: refuse unless the order really is this symbol",
    )
    @app_commands.choices(symbol=symbols)
    async def cancel_order(
        interaction: discord.Interaction, order_ticket: str, symbol: str | None = None
    ) -> None:
        await commands.cancel_order(interaction, order_ticket, symbol)

    @tree.command(name="close-trade", description="Close an open position")
    @app_commands.describe(
        position_id="The MT5 position id",
        volume="Volume to close; omit to close all of it",
        symbol="Optional: refuse unless the position really is this symbol",
    )
    @app_commands.choices(symbol=symbols)
    async def close_trade(
        interaction: discord.Interaction,
        position_id: str,
        volume: float | None = None,
        symbol: str | None = None,
    ) -> None:
        await commands.close_trade(interaction, position_id, volume, symbol)

    @tree.command(name="close", description="Close the open position on one symbol")
    @app_commands.describe(
        symbol="Which symbol",
        volume="Volume to close; omit to close all of it",
    )
    @app_commands.choices(symbol=symbols)
    async def close(
        interaction: discord.Interaction, symbol: str, volume: float | None = None
    ) -> None:
        await commands.close(interaction, symbol, volume)

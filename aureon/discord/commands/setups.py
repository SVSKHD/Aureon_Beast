"""``/setups`` and ``/setup``: what is being tracked, on demand (12, T-11).

The notifier's cards are pushed; these are pulled. Both render through ``build_setup_card``, so
there is exactly one description of a setup in this system and a card asked for on Tuesday looks
like the card that was posted on Monday.

Read-only, like every other read command: they touch ``setups`` and its events through a
repository and write nothing at all -- not the notification document either, because asking what a
setup is doing is not the same as announcing it, and a ``/setup`` call must not consume the claim
that stops the channel double-posting.
"""

from __future__ import annotations

import logging
from typing import Any

import discord
from discord import app_commands

from aureon.discord.context import BotContext
from aureon.discord.embeds import notice_embed, setup_embed
from aureon.discord.notifier import side_for
from aureon.discord.service import (
    build_setup_card,
    build_setup_confirmation,
    build_setup_trend_context,
    symbol_state_of,
)
from aureon.discord.views.setup_view import SetupView

log = logging.getLogger(__name__)

#: How many setups ``/setups`` lists. Ten, because the reply is one message and a list longer than
#: that is a scroll rather than an answer; the rest are reachable by ``/setup id``.
LIST_LIMIT = 10


async def setups_reply(context: Any, *, symbol: str, market_date: str | None = None) -> Any:
    """The open setups for one symbol, newest first, as one embed.

    Open ones only, from ``open_setups`` -- which filters on the state machine's own terminal set
    rather than on a second "closed" flag, so there is one answer to what terminal means. A
    completed setup is history and belongs to the review, not to a "what is live" list.
    """
    if context.setups is None:
        return notice_embed(
            "Setups unavailable",
            "This bot has no Firestore client, so it cannot read what is being tracked.",
            bad=True,
        )
    found = await context.run(
        context.setups.open_setups, symbol=symbol.upper(), market_date=market_date
    )
    if not found:
        return notice_embed(
            f"{symbol.upper()} · no open setups",
            "Nothing is being tracked for this symbol right now. That is a fact about the "
            "market, not a fault: most candles build nothing.",
        )

    newest = sorted(found, key=lambda one: one.opened_at, reverse=True)[:LIST_LIMIT]
    lines = [
        f"`{one.setup_id[:8]}` {one.family.value.replace('_', ' ')} · "
        f"{one.direction_context.value} · **{one.state.value}** · "
        f"anchor {one.anchor.price:g}"
        for one in newest
    ]
    hidden = len(found) - len(newest)
    if hidden:
        lines.append(f"…and {hidden} more; use `/setup id:` for any of them.")
    return notice_embed(f"{symbol.upper()} · {len(found)} open setup(s)", "\n".join(lines))


async def setup_reply(context: Any, *, setup_id: str) -> tuple[Any, Any]:
    """One setup's card and its view, or a notice and ``None``.

    Accepts the full id or the eight-character prefix the list shows, because the list shows a
    prefix and a human will paste what they can see. A prefix that matches more than one setup is
    refused by name rather than resolved to the first: picking one would show a card for a
    structure the reader was not asking about, and they would have no way to tell.
    """
    if context.setups is None:
        return (
            notice_embed(
                "Setups unavailable",
                "This bot has no Firestore client, so it cannot read what is being tracked.",
                bad=True,
            ),
            None,
        )

    setup = await context.run(context.setups.get, setup_id)
    if setup is None:
        setup = await _by_prefix(context, setup_id)
        if isinstance(setup, str):  # an ambiguity message
            return notice_embed("Ambiguous setup id", setup, bad=True), None
    if setup is None:
        return (
            notice_embed(
                "No such setup",
                f"Nothing is stored under `{setup_id}`. `/setups symbol:` lists what is open.",
                bad=True,
            ),
            None,
        )

    events = await context.run(context.setups.events, setup.setup_id)
    trend_context = None
    state = None
    if getattr(context, "system_state", None) is not None:
        state_doc = await context.run(
            context.system_state.read_symbol, setup.symbol, setup.timeframe
        )
        state = symbol_state_of(state_doc, setup.symbol)
        sessions = []
        if getattr(context, "sessions", None) is not None:
            sessions = await context.run(
                context.sessions.for_market_date,
                setup.market_date,
                symbol=setup.symbol,
            )
        trend_context = build_setup_trend_context(state, sessions)
    confirmation = build_setup_confirmation(
        setup,
        events=events,
        symbol_state=state,
        trend_context=trend_context,
    )
    screen = build_setup_card(
        setup,
        events=events,
        trend_context=trend_context,
        symbol_state=state,
    )
    view = SetupView(
        context,
        symbol=setup.symbol,
        setup_id=setup.setup_id,
        side=side_for(setup),
        cleared=confirmation.cleared,
    )
    return setup_embed(screen), view


async def _by_prefix(context: Any, prefix: str) -> Any:
    """Resolve an eight-character prefix against the open setups of every configured symbol."""
    if len(prefix) < 6:
        return None
    matches = []
    for symbol in context.config.symbols:
        for one in await context.run(context.setups.open_setups, symbol=symbol):
            if one.setup_id.startswith(prefix):
                matches.append(one)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        return (
            f"`{prefix}` matches {len(matches)} setups: "
            + ", ".join(f"`{one.setup_id}`" for one in matches[:5])
            + ". Use the full id."
        )
    return None


def register(tree: Any, context: BotContext) -> None:
    """``/setups`` and ``/setup``. Neither is authorization-gated.

    Deliberately: these READ observations, and §71's authorization gate is about who may move
    money. A channel readable by more people than may trade is exactly the situation the gate
    exists for, and gating a read of what the machine is watching would be security theatre that
    also hides the research from the people it is for. The buttons on the returned card are gated,
    as they are everywhere else.
    """

    @tree.command(
        name="setups",
        description="What structures are being tracked right now (observations, not signals)",
    )
    @app_commands.describe(symbol="Which instrument, e.g. XAUUSD")
    async def setups(interaction: discord.Interaction, symbol: str) -> None:
        await interaction.response.defer(thinking=True)
        embed = await setups_reply(context, symbol=symbol)
        await interaction.followup.send(embed=embed)

    @tree.command(
        name="setup",
        description="One setup's card: its state, its anchor, its events and its reference",
    )
    @app_commands.describe(id="A setup id, or the short prefix `/setups` lists")
    async def setup(interaction: discord.Interaction, id: str) -> None:  # noqa: A002
        await interaction.response.defer(thinking=True)
        embed, view = await setup_reply(context, setup_id=id)
        if view is None:
            await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send(embed=embed, view=view)


__all__ = ["LIST_LIMIT", "register", "setup_reply", "setups_reply"]

"""``/note`` — the trader's own words about a trade (§45, 9D).

    /note trade:last text:"chased it after the London open #london #chased"
    /note trade:3f9a1c text:"the setup was fine, the size was not"

Two things make this safe to allow on a CLOSED trade, which refuses every other field write:

* **the note is not written into the trade.** It goes to its own ``{prefix}_trade_notes``
  document keyed by ``trade_id``. A closed trade is a record of what happened, and editing
  one silently rewrites history (§45); a note is a sentence beside it, not a correction to
  it. A test plants a write to the trade document and turns red.
* **nothing automated ever reads it.** Not `/monitor`, not the cohort, not any gate. A
  boundary test keeps every module that computes a number from importing the note
  repository. The moment a note moved a number, the number would stop measuring the market
  and start measuring the trader's mood when they typed it — and nothing downstream could
  tell the two apart, because both arrive as a float.

`#tag`s in the text group a week's trades in the weekly review, by the trader's own
vocabulary rather than by anything Aureon invented. They are parsed from the sentence at
read time, not stored beside it.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import discord
from discord import app_commands

from aureon.discord.bot import requires_authorization
from aureon.discord.context import BotContext
from aureon.discord.embeds import notice_embed
from aureon.models.base import to_utc, utc_now

log = logging.getLogger(__name__)

#: What ``trade:last`` means. Deliberately not "the last trade you have a note on" and not
#: "the last open one": the most recently OPENED trade on this account, which is what
#: somebody typing it a minute after closing out is thinking of.
LAST = "last"

#: How far back ``trade:last`` looks. See ``_trade``.
LAST_WINDOW_DAYS = 7

MAX_NOTE = 2_000


class NoteCommands:
    def __init__(self, context: BotContext) -> None:
        self.context = context

    @requires_authorization
    async def note(
        self, interaction: discord.Interaction, trade: str, text: str
    ) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        context = self.context

        body = (text or "").strip()
        if not body:
            # A blank row under a trade is worse than no row: a reader next year would
            # wonder what was meant and have no way to find out.
            await self._refuse(interaction, "Nothing to record", "The note is empty.")
            return
        if len(body) > MAX_NOTE:
            await self._refuse(
                interaction,
                "Note too long",
                f"{len(body)} characters; the limit is {MAX_NOTE}.",
            )
            return

        if context.notes is None:
            await self._refuse(
                interaction, "Notes unavailable", "No note storage is configured."
            )
            return

        try:
            found = await self._trade(trade)
        except Exception:  # noqa: BLE001 - a note must not die silently
            log.exception("/note could not read trades")
            await self._refuse(
                interaction,
                "Notes unavailable",
                "Could not read trades from Firestore. Nothing was recorded.",
            )
            return

        if found is None:
            await self._refuse(
                interaction,
                "No such trade",
                f"No trade `{trade}` on this account."
                if trade != LAST
                else f"No trades opened on this account in the last {LAST_WINDOW_DAYS} "
                "days. Name a trade id to note an older one.",
            )
            return

        try:
            stored = await context.run(
                context.notes.add,
                found.trade_id,
                author=str(interaction.user.id),
                text=body,
            )
        except Exception:  # noqa: BLE001
            log.exception("/note could not store a note for %s", found.trade_id)
            await self._refuse(
                interaction, "Not recorded", "Could not write the note. Nothing changed."
            )
            return

        tags = ", ".join(f"#{tag}" for tag in stored.tags) or "none"
        await interaction.followup.send(
            embed=notice_embed(
                "Noted",
                f"On `{found.trade_id}` ({found.symbol}). Tags: {tags}.\n\n"
                "The trade itself is untouched — this is stored beside it, and the weekly "
                "review prints it under that trade with its outcome. Nothing automated "
                "reads it.",
            ),
            ephemeral=True,
        )

    async def _trade(self, which: str) -> Any:
        context = self.context
        if which != LAST:
            return await context.run(context.trades.get, which)

        # A bounded window rather than the whole collection: `last` is a convenience for
        # "the one I just closed", and a note on something from three weeks ago should name
        # its id rather than be reached by a word that means "recent".
        now = utc_now()
        recent = await context.run(
            context.trades.opened_in_period, now - timedelta(days=LAST_WINDOW_DAYS), now
        )
        if not recent:
            return None
        return max(recent, key=lambda t: to_utc(t.open_time.utc))

    async def _refuse(
        self, interaction: discord.Interaction, title: str, message: str
    ) -> None:
        await interaction.followup.send(
            embed=notice_embed(title, message, bad=True), ephemeral=True
        )


def register(tree: Any, context: BotContext) -> None:
    commands = NoteCommands(context)

    @tree.command(
        name="note",
        description="Your own words about a trade (stored beside it, never read by Aureon)",
    )
    @app_commands.describe(
        trade="A trade id, or `last` for the most recently opened",
        text="What you want to remember. #tags group trades in the weekly review",
    )
    async def note(interaction: discord.Interaction, trade: str, text: str) -> None:
        await commands.note(interaction, trade, text)

"""The Discord bot (§37-§47, §57, §59, §71).

A human interface over a backend that is already safe. Everything consequential was
decided before this file: the executor refuses anything the guard rejects, the repository
refuses a confirmation from the wrong user, and the monitor -- not Discord -- records what
happened. So the bot's job is to ask good questions, show honest numbers, and get out of
the way.

## Three rules every handler follows

* **defer within three seconds.** Discord discards an interaction that is not acknowledged
  in time, and a lost interaction looks to the human like the bot ignored them.
* **no Firestore on the event loop.** The client is synchronous; ``BotContext.run`` pushes
  every call to a thread. A blocked loop drops heartbeats and interactions alike.
* **allowlist first, before anything else happens.** Checked before any read, so an
  unauthorised user cannot even probe what exists (§71).

## What it cannot do

It holds no broker and no data provider (see ``BotContext``), imports neither, and a
boundary test enforces that. It writes only ``trade_requests``, ``control_requests``,
``settings.trading_enabled``, ``audit_logs`` and -- from 9C -- ``alerts`` and
``notifications``.

## The announcement loop (9C)

One background task, started once, posts detection embeds and fired reminders. It lives
here rather than in the notifier because only the client can turn a channel id into
something to send to; the notifier is handed an ``async send(...)`` and decides nothing
about transport. It starts only after the gateway is ready -- a channel cannot be fetched
before then -- and it never stops the bot: a sweep that raises is logged and the next one
runs.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import discord
from discord import app_commands

from aureon.discord.context import BotContext
from aureon.discord.embeds import notice_embed
from aureon.discord.notifier import Notifier
from aureon.discord.service import NotAuthorized, authorize

log = logging.getLogger(__name__)

T = TypeVar("T")


def requires_authorization(
    handler: Callable[..., Awaitable[None]],
) -> Callable[..., Awaitable[None]]:
    """Reject unauthorised users before the handler runs (§71).

    Deferring first, then rejecting, keeps the refusal ephemeral -- a rejection broadcast
    to a channel would be both noisy and a small information leak about who is allowed.
    """

    @functools.wraps(handler)
    async def wrapper(self: Any, interaction: discord.Interaction, *args: Any, **kwargs: Any):
        try:
            authorize(str(interaction.user.id), self.context.authorized_user_ids)
        except NotAuthorized as exc:
            log.warning("rejected %s: %s", interaction.user.id, exc)
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    embed=notice_embed("Not authorized", str(exc), bad=True), ephemeral=True
                )
            return None
        return await handler(self, interaction, *args, **kwargs)

    return wrapper


class AureonBot(discord.Client):
    """The Aureon Discord client."""

    def __init__(self, context: BotContext, **kwargs: Any) -> None:
        intents = kwargs.pop("intents", None) or discord.Intents.default()
        super().__init__(intents=intents, **kwargs)
        self.context = context
        self.tree = app_commands.CommandTree(self)
        self._guild = (
            discord.Object(id=context.config.discord_guild_id)
            if context.config.discord_guild_id
            else None
        )
        self.notifier = Notifier(context, send=self.announce)
        self._notifier_task: asyncio.Task[None] | None = None

    async def setup_hook(self) -> None:
        """Register commands, scoped to the configured guild (§71).

        Guild-scoped rather than global on purpose: a global command is visible in every
        server the application is added to, and this one places trades.
        """
        from aureon.discord.commands import register_all

        register_all(self.tree, self.context)
        if self._guild is not None:
            self.tree.copy_global_to(guild=self._guild)
            await self.tree.sync(guild=self._guild)
            log.info("commands synced to guild %s", self._guild.id)
        else:
            log.warning(
                "AUREON_DISCORD_GUILD_ID is not set; commands are NOT synced. Set it — a "
                "global trading command would appear in every server this app joins."
            )

        if self.context.config.alert_channel_id is None:
            # Announcing nothing is the right answer to "no channel chosen": the
            # alternative is a bot that picks a channel it can see and posts market calls
            # into it.
            log.warning(
                "AUREON_ALERT_CHANNEL_ID is not set; detections will NOT be announced"
            )
        elif self._notifier_task is None:
            self._notifier_task = asyncio.create_task(self._announce_forever())

    async def _announce_forever(self) -> None:
        """One task for the life of the process, started once.

        Waits for the gateway because a channel cannot be fetched before it. Started from
        ``setup_hook`` rather than ``on_ready``, which fires again on every reconnect --
        and a second loop would post everything twice for as long as both ran.
        """
        await self.wait_until_ready()
        log.info(
            "announcing detections in channel %s", self.context.config.alert_channel_id
        )
        await self.notifier.run()

    async def announce(
        self,
        target: int | str,
        *,
        embed: discord.Embed | None = None,
        view: discord.ui.View | None = None,
        direct: bool = False,
    ) -> None:
        """Send one announcement. The notifier's only way out of the process.

        ``direct`` sends to a user rather than a channel: a fired alert is one person's
        question, and the channel is readable by more people than armed it.
        """
        if direct:
            recipient: Any = self.get_user(int(target)) or await self.fetch_user(
                int(target)
            )
        else:
            recipient = self.get_channel(int(target)) or await self.fetch_channel(
                int(target)
            )
        await recipient.send(embed=embed, view=view)

    async def close(self) -> None:
        self.notifier.stop()
        if self._notifier_task is not None:
            self._notifier_task.cancel()
        await super().close()

    async def on_ready(self) -> None:  # pragma: no cover - requires a gateway
        log.info("connected as %s", self.user)

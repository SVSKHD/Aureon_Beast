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
import io
import logging
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import discord
from discord import app_commands

from aureon.discord.context import BotContext
from aureon.discord.embeds import notice_embed
from aureon.services.restart_notice import clear_restart_notice, read_restart_notice
from aureon.discord.notifier import Notifier
from aureon.discord.service import NotAuthorized, authorize, discord_cadences

log = logging.getLogger(__name__)

#: How often the bot re-reads the observer's published phase (11B). A minute: the thing it
#: is watching for happens twice a week, and the cost of noticing it a minute late is one
#: minute of the wrong cadence.
FOLLOW_SECONDS = 60.0

T = TypeVar("T")


class AureonCommandTree(app_commands.CommandTree):
    """Command tree that always acknowledges failed interactions.

    Discord shows "Application did not respond" when an exception escapes before a command
    acknowledges the interaction. Keep that transport failure separate from the underlying
    command error: log the real exception, then send a small ephemeral failure message whenever
    Discord still accepts a response.
    """

    async def on_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        command_name = getattr(getattr(interaction, "command", None), "qualified_name", "unknown")
        log.error(
            "Discord command /%s failed",
            command_name,
            exc_info=(type(error), error, error.__traceback__),
        )
        embed = notice_embed(
            "Command failed",
            "Aureon received the command but could not complete it. Check the local Aureon log "
            "for the underlying error.",
            bad=True,
        )
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except discord.HTTPException:
            log.exception("could not send Discord error response for /%s", command_name)


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
        self.tree = AureonCommandTree(self)
        self._guild = (
            discord.Object(id=context.config.discord_guild_id)
            if context.config.discord_guild_id
            else None
        )
        self.notifier = Notifier(context, send=self.announce, edit=self.revise)
        self._notifier_task: asyncio.Task[None] | None = None
        #: 11B. Optional: a bot without one behaves exactly as it did before, at the awake
        #: cadence all week. Set by ``main_discord`` when there is a heartbeat to slow.
        self.heartbeat: Any | None = None
        self._follow_task: asyncio.Task[None] | None = None
        #: The awake cadences, captured before anything slows them down.
        self._awake_poll = self.notifier.poll_seconds
        self._restart_notice_sent = False

    async def setup_hook(self) -> None:
        """Register commands and sync them where the operator asked.

        With ``AUREON_DISCORD_GUILD_ID`` set, commands are guild-scoped for immediate
        updates. Without it, the id is optional and commands are synced globally instead.
        Authorization remains enforced by the allowlist before handlers run.
        """
        from aureon.discord.commands import register_all

        register_all(self.tree, self.context)
        if self._guild is not None:
            self.tree.copy_global_to(guild=self._guild)
            await self.tree.sync(guild=self._guild)
            log.info("commands synced to guild %s", self._guild.id)
        else:
            await self.tree.sync()
            log.info(
                "AUREON_DISCORD_GUILD_ID is not set; commands synced globally "
                "(authorized-user checks still apply)"
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

        if self._follow_task is None:
            self._follow_task = asyncio.create_task(self._follow_the_market())

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

    async def _follow_the_market(self) -> None:
        """Slow this process down while the observer says the market is shut (11B).

        One state read a minute, and that is the whole cost. Discord does not decide whether
        the market is open -- it has no feed and may not call the broker -- so it follows
        the phase the observer publishes, which is also what ``/status`` and ``/execute``
        read. One source, one answer.

        A read failure is skipped rather than acted on: the cadences it would fall back to
        are the awake ones, and resetting them on every hiccup would undo the slowdown
        repeatedly over a weekend for no reason.
        """
        context = self.context
        config = context.config
        while not self.is_closed():
            try:
                state = await context.run(
                    context.system_state.read_symbol,
                    config.symbols[0],
                    config.timeframes[0],
                )
                cadences = discord_cadences(
                    state,
                    awake_heartbeat=config.state_heartbeat_seconds,
                    awake_poll=self._awake_poll,
                    sleep_heartbeat=config.sleep_heartbeat_seconds,
                    sleep_poll=config.sleep_poll_seconds,
                )
                self.notifier.poll_seconds = cadences.poll_seconds
                if self.heartbeat is not None:
                    self.heartbeat.set_interval(cadences.heartbeat_seconds)
            except Exception:  # noqa: BLE001 - following the market must not kill the bot
                log.exception("could not follow the market's phase")
            try:
                await asyncio.sleep(FOLLOW_SECONDS)
            except asyncio.CancelledError:
                return

    async def announce(
        self,
        target: int | str,
        *,
        embed: discord.Embed | None = None,
        view: discord.ui.View | None = None,
        direct: bool = False,
        chart: bytes | None = None,
        filename: str | None = None,
    ) -> str | None:
        """Send one announcement. The notifier's only way out of the process.

        ``direct`` sends to a user rather than a channel: a fired alert is one person's
        question, and the channel is readable by more people than armed it.

        Returns the message id as a STRING (12, T-11), so a setup's card can be edited later. A
        string because a Discord snowflake exceeds 2^53 and a JSON round trip through a float
        would corrupt one -- which would send a later edit to the wrong message.
        """
        recipient = await self._recipient(target, direct=direct)
        message = await recipient.send(embed=embed, view=view, **_attachment(chart, filename))
        return None if message is None else str(message.id)

    async def revise(
        self,
        target: int | str,
        message_id: str,
        *,
        embed: discord.Embed | None = None,
        view: discord.ui.View | None = None,
        chart: bytes | None = None,
        filename: str | None = None,
    ) -> None:
        """Edit a message already in the channel (12, T-11).

        Re-fetched by id rather than cached: the bot restarts, and the ``discord.Message`` object
        from before the restart is gone while the message in the channel is not. The id is stored
        on the notification document precisely so this can be done from a cold start.

        A missing message -- deleted by a human, or in a channel the bot has lost -- raises, and
        the caller records the failure without retrying. Re-posting would leave two cards for one
        setup, which is the thing this whole design exists to avoid.
        """
        recipient = await self._recipient(target, direct=False)
        message = await recipient.fetch_message(int(message_id))
        await message.edit(
            embed=embed, view=view, **_attachment(chart, filename, editing=True)
        )

    async def _recipient(self, target: int | str, *, direct: bool) -> Any:
        if direct:
            return self.get_user(int(target)) or await self.fetch_user(int(target))
        return self.get_channel(int(target)) or await self.fetch_channel(int(target))

    async def close(self) -> None:
        self.notifier.stop()
        if self._notifier_task is not None:
            self._notifier_task.cancel()
        if self._follow_task is not None:
            self._follow_task.cancel()
        await super().close()

    async def _announce_restart_notice(self) -> None:
        """Post the durable reason left by the supervisor before a self-restart."""
        if self._restart_notice_sent:
            return
        repo_root = Path(__file__).resolve().parents[2]
        notice = read_restart_notice(repo_root)
        if notice is None:
            self._restart_notice_sent = True
            return
        channel_id = self.context.config.alert_channel_id
        if channel_id is None:
            log.warning(
                "runtime restart notice is pending but AUREON_ALERT_CHANNEL_ID is not set"
            )
            return

        reason = str(notice.get("reason") or "planned restart")
        detail = str(notice.get("detail") or "")
        old_sha = notice.get("old_sha")
        new_sha = notice.get("new_sha")
        lines = [f"**Reason:** {reason}"]
        if detail:
            lines.append(detail)
        if old_sha and new_sha:
            lines.append(f"`{str(old_sha)[:12]} → {str(new_sha)[:12]}`")
        try:
            await self.announce(
                channel_id,
                embed=notice_embed("Aureon restarted", "\n".join(lines)),
            )
        except Exception:  # noqa: BLE001 - retain the file so the next reconnect can retry
            log.exception("could not post runtime restart notice")
            return

        clear_restart_notice(repo_root)
        self._restart_notice_sent = True

    async def on_ready(self) -> None:  # pragma: no cover - requires a gateway
        log.info("connected as %s", self.user)
        await self._announce_restart_notice()


def _attachment(
    chart: bytes | None, filename: str | None, *, editing: bool = False
) -> dict[str, Any]:
    """The keyword a chart needs, or nothing at all.

    The two call sites genuinely differ: a send takes ``file`` and an edit takes
    ``attachments``. Getting that backwards is not a crash -- discord.py accepts ``file`` on an
    edit and ignores it -- so the words would update while the picture silently stayed the old
    one, which is a card that looks stale in exactly the way a reader cannot diagnose. Hence the
    flag, and hence this being one function rather than a keyword spelled out twice.

    On an edit with no chart, ``attachments=[]`` CLEARS whatever was there. That is deliberate: a
    setup whose chart can no longer be drawn should lose its picture rather than keep one from
    twenty candles ago.
    """
    if not chart or not filename:
        return {"attachments": []} if editing else {}
    attachment = discord.File(io.BytesIO(chart), filename=filename)
    return {"attachments": [attachment]} if editing else {"file": attachment}

"""Announcing detections and fired alerts in a channel (9C).

A loop inside the Discord process: every few seconds it reads the detections written in the
last couple of minutes, and the alerts that fired, and posts the ones it has not posted
before.

## Polling Firestore rather than listening to it

The Firestore Python client does offer snapshot listeners. This polls instead, for three
reasons that all point the same way:

* a listener's callback runs on the client's own thread, and posting to Discord from it means
  hopping back onto the event loop anyway;
* a dropped listener reconnects **silently**, and the window it missed is gone -- where a poll
  that fails simply runs again with the same window and finds what it missed;
* the window is the dedup's backstop. Reading "the last two minutes" and skipping what is
  already recorded means a restart re-reads a little and posts nothing, which is exactly the
  behaviour wanted from a restart.

Two minutes is long enough to survive a restart or a slow write, and short enough that a bot
which was down for an hour wakes up and posts nothing rather than an hour of history at once
-- a channel full of stale detections is worse than a gap, because the gap is visible.

## Discord still computes nothing

Every field in a detection embed comes from the stored detection, and every field in a
reminder comes from the snapshot the observer froze. This module decides *whether* to post
and *what has been posted*; it never decides what is true.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from aureon.discord.context import BotContext
from aureon.discord.embeds import notification_embed, reminder_embed
from aureon.discord.service import build_notification, build_reminder, should_notify
from aureon.discord.views.notification_view import NotificationView
from aureon.models.alerts import PriceAlert
from aureon.models.base import to_utc, utc_now
from aureon.models.detection import Detection
from aureon.models.enums import NotificationKind

log = logging.getLogger(__name__)

DEFAULT_POLL_SECONDS = 5.0


@dataclass
class Posted:
    """What one sweep announced, for the tests and the log."""

    detections: list[str]
    reminders: list[str]

    @property
    def total(self) -> int:
        return len(self.detections) + len(self.reminders)


class Notifier:
    """Posts detection embeds and fired reminders, exactly once each (9C)."""

    def __init__(
        self,
        context: BotContext,
        *,
        send: Any,
        channel_id: int | str | None = None,
        window_seconds: float | None = None,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
    ) -> None:
        self.context = context
        #: ``async send(channel_id, embed=..., view=...)``. Injected rather than reaching
        #: for a discord.Client here, so every decision in this module is testable without
        #: a gateway -- the same split the commands use.
        self.send = send
        self.channel_id = channel_id or context.config.alert_channel_id
        self.window_seconds = (
            window_seconds
            if window_seconds is not None
            else context.config.notify_window_seconds
        )
        self.poll_seconds = poll_seconds
        self._stop = asyncio.Event()

    # ── One sweep ─────────────────────────────────────────────────────────────

    async def sweep(self, *, now: datetime | None = None) -> Posted:
        """Announce whatever is new. Safe to call as often as you like."""
        moment = to_utc(now or utc_now())
        if self.channel_id is None:
            # No channel chosen: Aureon announces nothing rather than picking one.
            return Posted([], [])
        posted = Posted([], [])
        posted.detections = await self._sweep_detections(moment)
        posted.reminders = await self._sweep_reminders(moment)
        if posted.total:
            log.info(
                "announced %d detection(s) and %d reminder(s)",
                len(posted.detections),
                len(posted.reminders),
            )
        return posted

    async def _sweep_detections(self, now: datetime) -> list[str]:
        context = self.context
        if context.notifications is None or context.notification_settings is None:
            return []
        settings = await context.run(context.notification_settings.read_or_default)
        since = now - timedelta(seconds=self.window_seconds)

        announced: list[str] = []
        for symbol in context.config.symbols:
            recent = await context.run(
                context.detections.recent_for_symbol, symbol, since=since, limit=25
            )
            # Oldest first, so a burst reads in the order it happened.
            for detection in sorted(recent, key=lambda d: d.detected_at.utc):
                if not should_notify(detection, settings):
                    continue
                if await self._post_detection(detection, now=now):
                    announced.append(detection.detection_id)
        return announced

    async def _post_detection(self, detection: Detection, *, now: datetime) -> bool:
        context = self.context
        claim = await context.run(
            context.notifications.claim,
            NotificationKind.DETECTION,
            detection.detection_id,
            symbol=detection.symbol,
            channel_id=str(self.channel_id),
            now=now,
        )
        if claim is None:
            return False  # already announced, here or by a previous process
        screen = build_notification(detection)
        try:
            await self.send(
                self.channel_id,
                embed=notification_embed(screen),
                view=NotificationView(
                    context,
                    symbol=detection.symbol,
                    detection_id=detection.detection_id,
                    side=screen.side,
                ),
            )
        except Exception as exc:  # noqa: BLE001 - recorded, not retried (9C-1)
            log.exception("could not post detection %s", detection.detection_id)
            await context.run(
                context.notifications.mark_failed,
                NotificationKind.DETECTION,
                detection.detection_id,
                message=str(exc),
            )
            return False
        return True

    async def _sweep_reminders(self, now: datetime) -> list[str]:
        context = self.context
        if context.alerts is None or context.notifications is None:
            return []
        fired = await context.run(self._recent_fired, now)
        announced: list[str] = []
        for alert in fired:
            if await self._post_reminder(alert, now=now):
                announced.append(alert.alert_id)
        return announced

    def _recent_fired(self, now: datetime) -> list[PriceAlert]:
        """Alerts that fired inside the window, oldest first.

        The window is what stops a restart re-announcing yesterday: ``fired_since`` would
        happily return every FIRED alert ever, and an alert stays FIRED until it is
        deleted. Dedup then makes the second post a no-op, but only for alerts this
        deployment has a notification document for -- so the window, not the claim, is what
        keeps a fresh project from posting a year of history on its first sweep.
        """
        since = now - timedelta(seconds=self.window_seconds)
        return list(self.context.alerts.fired_since(since))

    async def _post_reminder(self, alert: PriceAlert, *, now: datetime) -> bool:
        context = self.context
        claim = await context.run(
            context.notifications.claim,
            NotificationKind.ALERT,
            alert.alert_id,
            symbol=alert.symbol,
            channel_id=str(alert.requested_by),
            now=now,
        )
        if claim is None:
            return False
        screen = build_reminder(alert)
        try:
            await self.send(
                # To the requester, not the channel: an alert is one person's question, and
                # the channel is readable by more people than armed it.
                alert.requested_by,
                embed=reminder_embed(screen),
                view=NotificationView(
                    context, symbol=alert.symbol, side=screen.side, detection_id=None
                ),
                direct=True,
            )
        except Exception as exc:  # noqa: BLE001 - recorded, not retried
            log.exception("could not post reminder %s", alert.alert_id)
            await context.run(
                context.notifications.mark_failed,
                NotificationKind.ALERT,
                alert.alert_id,
                message=str(exc),
            )
            return False
        return True

    # ── The loop ──────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Sweep until stopped. A failure is logged and the loop continues."""
        while not self._stop.is_set():
            try:
                await self.sweep()
            except Exception:  # noqa: BLE001 - a sweep failure must not kill the bot
                log.exception("notification sweep failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
            except TimeoutError:
                continue

    def stop(self) -> None:
        self._stop.set()

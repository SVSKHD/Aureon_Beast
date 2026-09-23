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
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from aureon.discord.context import BotContext
from aureon.discord.embeds import notification_embed, reminder_embed, setup_embed
from aureon.discord.service import (
    build_notification,
    build_reminder,
    build_setup_card,
    build_setup_confirmation,
    build_setup_trend_context,
    should_notify,
    symbol_state_of,
)
from aureon.discord.views.notification_view import NotificationView
from aureon.discord.views.setup_view import SetupView
from aureon.models.alerts import PriceAlert
from aureon.models.base import to_utc, utc_now
from aureon.models.detection import Detection
from aureon.models.enums import DirectionContext, NotificationKind

log = logging.getLogger(__name__)

DEFAULT_POLL_SECONDS = 5.0


@dataclass
class Posted:
    """What one sweep announced, for the tests and the log."""

    detections: list[str]
    reminders: list[str]
    #: 12 T-11. Setups whose card was posted OR edited this sweep. One list for both, because a
    #: card is one message for the life of the setup and "we said something about this setup"
    #: is the fact worth counting; whether it was the first thing said is in the log.
    setups: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.detections) + len(self.reminders) + len(self.setups)


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
        edit: Any = None,
        charts: bool = True,
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
        #: ``async edit(channel_id, message_id, embed=..., view=..., chart=..., filename=...)``.
        #: Injected like ``send``, so every decision below is testable without a gateway. A
        #: notifier constructed without one posts cards and never edits them, which is why the
        #: default is not silently a no-op: ``_post_setup`` treats a missing ``edit`` as a
        #: configuration error and says so once.
        self.edit = edit
        #: 12 T-10/T-11. False renders no chart at all -- for a deployment without the ``charts``
        #: extra, and for the tests, which assert the card's words rather than its picture.
        self.charts = charts
        # Do not bulk-refresh every open card on startup. A restart used to PATCH every
        # historical open setup in one burst and hit Discord rate limits. Cards now update
        # only when the setup itself changes.
        self._refresh_open_setups = False
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
        posted.setups = await self._sweep_setups(moment)
        if posted.total:
            log.info(
                "announced %d detection(s), %d reminder(s) and %d setup card(s)",
                len(posted.detections),
                len(posted.reminders),
                len(posted.setups),
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

    # ── 12 T-11: setup cards, one message per setup ───────────────────────────

    async def _sweep_setups(self, now: datetime) -> list[str]:
        """Post or EDIT one card per setup that changed inside the window.

        ## One message, edited, rather than one message per transition

        A setup that walks OBSERVING → WATCH → DEVELOPING → CONFIRMED → PULLBACK → CONTINUATION
        → COMPLETED is ONE thing that happened, and seven messages about it is a channel nobody
        reads and a scroll a human has to reassemble. So the first announced state posts a card
        and every later one edits it in place: the channel holds one live card per setup, showing
        where it is now, with its last few events as the story of how it got there.

        ## The claim-before-post tradeoff, and what it costs here

        ``notifications.claim`` is a Firestore ``create``: exactly one process may take the right
        to post a given setup, which is what stops two Discord instances double-posting. The cost
        is the one 9C already accepted -- **a claim that succeeds and then fails to post is never
        retried.** For a detection that means one missing embed. For a setup it means the card for
        that setup never appears at all, because every later transition takes the edit path and
        finds no ``message_id``.

        That is the right trade anyway: the alternative is retrying a post whose outcome is
        unknown, which double-posts the one message a human presses a button on. The failure is
        recorded on the notification document for the operator, and the setup itself is in
        Firestore regardless -- ``/setup id`` renders the same card on demand.

        ## Restart

        The ``message_id`` lives on the notification document, so a restarted process reads it
        back and keeps editing the same message. It is NOT on the setup: Discord does not own
        ``setups`` (§71), and "what have we already said about this" is exactly what the
        notifications collection is for.
        """
        context = self.context
        if context.setups is None or context.notifications is None:
            return []
        if context.notification_settings is None:
            return []
        settings = await context.run(context.notification_settings.read_or_default)
        since = now - timedelta(seconds=self.window_seconds)

        announced: list[str] = []
        for symbol in context.config.symbols:
            changed = await context.run(
                context.setups.changed_since, symbol=symbol, since=since
            )
            candidates = {setup.setup_id: setup for setup in changed}

            # Oldest first, so a burst reads in the order it happened.
            for setup in sorted(
                candidates.values(), key=lambda one: (one.updated_at or one.opened_at)
            ):
                trigger = await self._setup_trigger(setup)
                # Existing cards are refreshed even if this state's announcement setting is now
                # off; the message already exists and needs current presentation. New cards still
                # obey notification settings.
                existing = await context.run(
                    context.notifications.get, NotificationKind.SETUP, setup.setup_id
                )
                if existing is None and not settings.announces_setup(trigger):
                    continue
                if await self._post_setup(setup, now=now):
                    announced.append(setup.setup_id)
        return announced

    async def _setup_trigger(self, setup: Any) -> str:
        """What this change should be judged by: the state, or the descriptive event that caused it.

        A state change is announced under its STATE. A descriptive ``WATCH_*`` event leaves the
        state alone, so judging it by the state would announce it under whatever the setup already
        was -- and a reader could not switch off "tell me about repeated level tests" without also
        switching off the state those tests happen in.
        """
        context = self.context
        last = setup.last_event_id
        if last is None:
            return setup.state.value
        event = await context.run(context.setups.get_event, setup.setup_id, last)
        if event is None:
            return setup.state.value
        if event.to_state is event.from_state:
            return event.event_type.value
        return setup.state.value

    async def _post_setup(self, setup: Any, *, now: datetime) -> bool:
        context = self.context
        existing = await context.run(
            context.notifications.get, NotificationKind.SETUP, setup.setup_id
        )
        events = await context.run(context.setups.events, setup.setup_id)

        state_doc = await context.run(
            context.system_state.read_symbol,
            setup.symbol,
            setup.timeframe,
        )
        symbol_state = symbol_state_of(state_doc, setup.symbol)
        sessions = (
            await context.run(
                context.sessions.for_market_date,
                setup.market_date,
                symbol=setup.symbol,
            )
            if context.sessions is not None
            else []
        )
        trend_context = build_setup_trend_context(symbol_state, sessions)
        confirmation = build_setup_confirmation(
            setup,
            events=events,
            symbol_state=symbol_state,
            trend_context=trend_context,
        )
        detections = (
            await context.run(
                context.detections.for_market_date,
                setup.symbol,
                setup.market_date,
            )
            if context.detections is not None
            else []
        )
        chart, filename = await self._setup_chart(
            setup,
            events,
            trend_context,
            confirmation,
            detections,
        )
        screen = build_setup_card(
            setup,
            events=events,
            chart_filename=filename,
            trend_context=trend_context,
            symbol_state=symbol_state,
        )
        embed = setup_embed(screen)
        view = SetupView(
            context,
            symbol=setup.symbol,
            setup_id=setup.setup_id,
            side=side_for(setup),
            cleared=confirmation.cleared,
        )

        if existing is None:
            claim = await context.run(
                context.notifications.claim,
                NotificationKind.SETUP,
                setup.setup_id,
                symbol=setup.symbol,
                channel_id=str(self.channel_id),
                now=now,
            )
            if claim is None:
                return False  # another process took it between the read and the claim
            try:
                message_id = await self.send(
                    self.channel_id, embed=embed, view=view, chart=chart, filename=filename
                )
            except Exception as exc:  # noqa: BLE001 - recorded, not retried (9C-1)
                log.exception("could not post the card for setup %s", setup.setup_id)
                await context.run(
                    context.notifications.mark_failed,
                    NotificationKind.SETUP,
                    setup.setup_id,
                    message=str(exc),
                )
                return False
            if message_id is not None:
                await context.run(
                    context.notifications.record_message,
                    NotificationKind.SETUP,
                    setup.setup_id,
                    message_id=str(message_id),
                )
            return True

        if self.edit is None:
            # Constructed without an editor. Said once rather than silently skipped: a card that
            # posted and then never changed again looks live and is not, and the operator cannot
            # tell from the channel which of the two it is.
            log.error(
                "the notifier has no edit callable; the card for setup %s cannot be updated",
                setup.setup_id,
            )
            return False
        if existing.message_id is None:
            # Claimed, never posted. Not retried -- see the docstring. Saying so once per sweep
            # would fill the log, so this is DEBUG and the FAILED row is the operator's signal.
            log.debug(
                "setup %s was claimed but never posted; not retrying", setup.setup_id
            )
            return False
        try:
            await self.edit(
                self.channel_id,
                existing.message_id,
                embed=embed,
                view=view,
                chart=chart,
                filename=filename,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("could not edit the card for setup %s", setup.setup_id)
            await context.run(
                context.notifications.mark_failed,
                NotificationKind.SETUP,
                setup.setup_id,
                message=str(exc),
            )
            return False
        return True

    async def _setup_chart(
        self,
        setup: Any,
        events: Any = (),
        trend_context: Any = None,
        confirmation: Any = None,
        detections: Any = (),
    ) -> tuple[bytes | None, str | None]:
        """The chart to attach, or ``(None, None)``.

        Never raises and never blocks past the renderer's own budget: a card with its words and
        no picture is a card; a sweep that died drawing one is a channel that stops updating. The
        renderer is handed bars from ``market_day_frames`` and the symbol's stored spec, and it
        computes nothing -- see ``aureon/visuals/chart_renderer.py``.
        """
        context = self.context
        if context.market_days is None or not self.charts:
            return None, None
        try:
            from aureon.visuals import chart_renderer

            bars = await context.run(
                chart_renderer.bars_for,
                context.market_days,
                symbol=setup.symbol,
                timeframe=setup.timeframe,
                market_dates=[setup.market_date],
                include_today=setup.market_date,
            )
            spec = await context.run(context.symbols.get, setup.symbol)
            png = await context.run(
                _render_chart,
                setup,
                bars,
                spec,
                events,
                trend_context,
                confirmation,
                detections,
            )
        except Exception:  # noqa: BLE001 - a missing picture must not cost the card
            log.exception("could not draw the chart for setup %s", setup.setup_id)
            return None, None
        if not png:
            return None, None
        return png, f"{setup.symbol}_{setup.timeframe.value}_{setup.setup_id[:8]}.png"

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


#: 12 T-11. BULLISH prefills BUY and BEARISH prefills SELL on the card's ``[Execute]`` button;
#: NEUTRAL prefills nothing and the button is removed.
#:
#: This is the one place this system maps a DESCRIPTION onto an ACTION, and it is written here as
#: a table rather than buried in an ``if`` so that it can be read, argued with and changed in one
#: edit. What it buys is a retyping error removed; what it must never buy is a decision made. The
#: lot is still typed into a modal, the order is still planned by ``plan_market_order``, CONFIRM
#: is still required, and Discord still never calls the broker.
SIDE_FOR_CONTEXT: dict[str, str] = {
    DirectionContext.BULLISH.value: "buy",
    DirectionContext.BEARISH.value: "sell",
}


def side_for(setup: Any) -> str | None:
    """The side a setup's card offers, or ``None`` for NEUTRAL. See ``SIDE_FOR_CONTEXT``."""
    return SIDE_FOR_CONTEXT.get(setup.direction_context.value)


def _render_chart(
    setup: Any,
    bars: Any,
    spec: Any,
    events: Any = (),
    trend_context: Any = None,
    confirmation: Any = None,
    detections: Any = (),
) -> bytes:
    """Draw a setup chart from stored candles and frozen setup-event context.

    Discord still computes no indicators. EMA/RSI values and event labels come from the
    setup-event snapshots written by the observer; the picture only renders those facts.
    """
    from aureon.discord.service import (
        _directional_early_ema,
        _early_ema_status,
        _ema_relation,
        _event_snapshot_float,
        _latest_snapshot_event,
        _rsi_status,
    )
    from aureon.services.setup_reference import render_reference
    from aureon.visuals import chart_renderer

    event_list = list(events)
    latest = _latest_snapshot_event(event_list)
    fast = _event_snapshot_float(latest, "ema_fast") if latest is not None else None
    slow = _event_snapshot_float(latest, "ema_slow") if latest is not None else None
    snapshot = getattr(latest, "context_snapshot", None) or {} if latest is not None else {}
    previous = None
    if latest is not None:
        from aureon.discord.service import _previous_snapshot_event
        previous = _previous_snapshot_event(event_list, latest)
    prev_fast = _event_snapshot_float(previous, "ema_fast") if previous is not None else None
    prev_slow = _event_snapshot_float(previous, "ema_slow") if previous is not None else None
    directional_early = _directional_early_ema(prev_fast, prev_slow, fast, slow)

    levels = []
    if fast is not None:
        levels.append(chart_renderer.ChartLevel(price=fast, label="EMA20 now"))
    if slow is not None:
        levels.append(chart_renderer.ChartLevel(price=slow, label="EMA50 now"))

    marks = []
    for event in event_list[-8:]:
        close = _event_snapshot_float(event, "close")
        if close is None:
            continue
        marks.append(
            chart_renderer.ChartMark(
                at=event.market_time.utc,
                price=close,
                label=event.event_type.value.replace("_", " "),
                direction_context=getattr(setup.direction_context, "value", None),
            )
        )

    detection_marks = []
    for detection in detections:
        if getattr(detection, "timeframe", None) != setup.timeframe:
            continue
        direction = getattr(getattr(detection, "direction", None), "value", None)
        direction_context = (
            "bullish" if direction == "buy"
            else "bearish" if direction == "sell"
            else None
        )
        session = getattr(
            getattr(getattr(detection, "session", None), "session", None),
            "value",
            "unknown",
        )
        if detection.agent_name == "ema_cross":
            label = (
                f"BULL CROSS · {session}"
                if direction == "buy"
                else f"BEAR CROSS · {session}"
                if direction == "sell"
                else f"EMA CROSS · {session}"
            )
        elif detection.agent_name == "wick":
            label = f"WICK · {detection.event_key.replace('_', ' ')}"
        elif detection.agent_name == "liquidity":
            label = f"LIQ · {detection.event_key.replace('_', ' ')}"
        elif detection.agent_name == "breakout":
            label = f"BREAK · {detection.event_key.replace('_', ' ')}"
        else:
            continue
        detection_marks.append(
            chart_renderer.ChartMark(
                at=detection.candle_open_time.utc,
                price=detection.price,
                label=label,
                direction_context=direction_context,
            )
        )

    notes = [render_reference(setup.reference)[0]]
    if latest is not None:
        notes.extend(
            [
                f"EMA: {_ema_relation(fast, slow)} · {directional_early}",
                f"RSI: {_rsi_status(event_list, latest)}",
                f"setup snapshot trend: {snapshot.get('trend', 'unknown')} · mtf {snapshot.get('mtf_alignment', 'unknown')}",
            ]
        )

    analysis_lines = []
    if trend_context is not None:
        analysis_lines.extend(
            [
                f"PRESENT  {trend_context.present}",
                f"ASIA     {trend_context.asia}",
                f"LONDON   {trend_context.london}",
            ]
        )
        if trend_context.evidence:
            analysis_lines.append("EVIDENCE")
            analysis_lines.extend(
                f"• {line[:52]}" for line in trend_context.evidence[:4]
            )
    if confirmation is not None:
        analysis_lines.append(f"CLEAR    {confirmation.clearance}")
        analysis_lines.append("MTF")
        analysis_lines.extend(f"  {line}" for line in confirmation.mtf[:5])
        if confirmation.blockers:
            analysis_lines.append("BLOCK")
            analysis_lines.extend(f"• {line[:48]}" for line in confirmation.blockers[:3])
    if latest is not None:
        analysis_lines.extend(
            [
                f"EMA      {_ema_relation(fast, slow)}",
                f"EARLY    {directional_early}",
                f"RSI      {_rsi_status(event_list, latest)}",
                f"SNAPSHOT {snapshot.get('trend', 'unknown')}",
            ]
        )

    overlays = chart_renderer.Overlays(
        title=(
            f"{setup.symbol} {setup.timeframe.value} · "
            f"{setup.family.value.replace('_', ' ')} · "
            f"{setup.direction_context.value.upper()}"
        ),
        subtitle=(
            f"{setup.state.value} · "
            + (confirmation.clearance if confirmation is not None else "confirmation unknown")
        ),
        levels=tuple(levels),
        anchor_price=setup.anchor.price,
        invalidation_price=setup.invalidation_price,
        detections=tuple(detection_marks),
        events=tuple(marks),
        notes=tuple(notes),
        analysis_lines=tuple(analysis_lines),
    )
    return chart_renderer.render(
        symbol=setup.symbol,
        timeframe=setup.timeframe,
        bars=bars,
        spec=spec,
        overlays=overlays,
    )

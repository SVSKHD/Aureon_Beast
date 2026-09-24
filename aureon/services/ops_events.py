"""The named operational conditions, and the once-on-onset once-on-recovery rule (11A, F-15).

The failure this exists for is mostly not a crash -- ``aureon/services/supervisor.py`` handles
those, and records the one condition here that it owns (``supervisor_stack_down``). It is a
service still running and no longer doing its job: an observer whose candle loop stopped
while the market is open, an outbox whose deliveries have been failing for a minute, a
reconciliation that ended ambiguous and left a trade nobody has looked at. None of those
produces a log line anybody reads or an alert anybody gets.

## The rule, and why it is the whole design

Each condition is posted **once when it starts and once when it clears**. Never on every poll.

A condition that persists for six hours would otherwise produce six hours of identical
messages, an operator would mute the channel, and a muted channel is strictly worse than no
channel — it looks like coverage. So ``observe`` is called as often as the caller likes, with
the current truth, and returns a message only on a transition.

## Conditions, not moments

Every event here is phrased as something that is either true or false right now, including the
two that sound like moments:

* ``mt5_reconnect`` is "the terminal connection is currently being re-established" — onset when
  a reconnect starts, clear when it succeeds. A moment with no recovery would tell an operator
  a reconnect happened and never that it worked, which is the half of the story that matters at
  03:00.
* ``live_account_detected`` is "this process is talking to a real-money account". It clears only
  if the account changes, which needs a restart, and that is honest: the condition really is
  still true.

## Thresholds live in config, and the register does not compute them

``observe(name, active=...)`` takes a boolean the CALLER worked out. The register owns the
once-and-once rule and nothing else. That split is deliberate: "has the observer gone stale" is
a question about candle times and market state that only the observer can answer, and a register
that tried to answer it would need the observer's whole world handed to it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from aureon.models.base import to_utc, utc_now
from aureon.models.ops import OpsEvent

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class OpsEventSpec:
    """One named condition: what it means, and what to say at each edge."""

    name: str
    #: What is wrong, in a sentence an operator can act on at 03:00 without the codebase.
    onset: str
    #: What is no longer wrong. Stated separately rather than as "X cleared", because the
    #: useful recovery message often names what to check anyway.
    recovery: str
    #: True when one condition per instrument is the honest shape. Silver's feed going quiet
    #: says nothing about gold's, and a single event for both would clear the moment either
    #: recovered -- reporting healthy while one symbol was still dark.
    scoped: bool = False


#: Every condition Aureon announces. A closed set on purpose: a register that accepted any
#: string would grow a second vocabulary of near-duplicates ("observer_stale",
#: "observer_is_stale") that no runbook could document, and ``/ops`` would show a list nobody
#: could read. Adding one is a deliberate edit here, with its sentence written at the same time.
SPECS: tuple[OpsEventSpec, ...] = (
    OpsEventSpec(
        "observer_stale",
        "no candle has closed for longer than two timeframes while the market is OPEN — "
        "the observer is running and not observing",
        "candles are closing again",
    ),
    OpsEventSpec(
        "executor_stale",
        "the executor's heartbeat has stopped while the market is OPEN — confirmed requests "
        "will sit unexecuted",
        "the executor is beating again",
    ),
    OpsEventSpec(
        "monitor_stale",
        "the position monitor's heartbeat has stopped — open positions are not being "
        "reconciled against the broker",
        "the position monitor is beating again",
    ),
    OpsEventSpec(
        "firestore_unavailable",
        "outbox delivery has been failing for longer than the threshold — detections are "
        "queued on disk and nothing downstream can see them",
        "Firestore writes are succeeding again; the queue will drain",
    ),
    OpsEventSpec(
        "outbox_backlog",
        "the outbox has more undelivered rows than the threshold — deliveries are slower "
        "than detections are arriving",
        "the outbox backlog is back under the threshold",
    ),
    OpsEventSpec(
        "reconciliation_ambiguous",
        "a request ended FAILED_RECONCILIATION: the broker's answer could not be matched to "
        "it, so whether an order exists is UNKNOWN and needs a human at the terminal",
        "no request is left in FAILED_RECONCILIATION",
    ),
    OpsEventSpec(
        "archive_write_failed",
        "a candle could not be written to the parquet archive — the session will have a gap "
        "and live-vs-replay parity for it cannot be checked afterwards",
        "archive writes are succeeding again",
    ),
    OpsEventSpec(
        "symbol_feed_stale",
        "no tick for longer than the threshold while the market is OPEN — this symbol's feed "
        "is dark and its quotes are stale",
        "ticks are arriving again for this symbol",
        scoped=True,
    ),
    OpsEventSpec(
        "market_holiday",
        "the weekly market is closed and the last tick has been quiet beyond the guardian "
        "threshold — this is expected weekend silence and automatic restart is suppressed",
        "the market is no longer in the confirmed weekend-holiday state",
        scoped=True,
    ),
    OpsEventSpec(
        "mt5_reconnect",
        "the MT5 connection dropped and is being re-established",
        "the MT5 connection is back",
    ),
    OpsEventSpec(
        "live_account_detected",
        "this process is connected to a real-money account",
        "this process is connected to a practice account",
    ),
    OpsEventSpec(
        "supervisor_stack_down",
        "a service process exited and the launcher stopped the rest — Aureon is NOT running",
        "the full stack is running again",
    ),
)

BY_NAME: dict[str, OpsEventSpec] = {spec.name: spec for spec in SPECS}


class UnknownOpsEvent(KeyError):
    """A name that is not in ``SPECS``. Raised rather than accepted -- see SPECS."""


@dataclass
class Posted:
    """What one ``observe`` call decided, for the caller's log and the tests."""

    name: str
    scope: str | None
    #: "onset", "recovery", or None when nothing changed.
    edge: str | None
    message: str | None


class OpsRegister:
    """Turns a stream of current-truth observations into edges (11A, F-15).

    ``post`` is an injected ``callable(message)`` -- normally the ops channel, a log line in a
    service that has no Discord client. Injected for the reason every other outbound call in
    this codebase is: the rule under test is *when* a message is sent, and that is testable
    without a gateway.
    """

    def __init__(
        self,
        repository: Any,
        *,
        service: str,
        post: Any | None = None,
        now: Any = utc_now,
    ) -> None:
        self.repository = repository
        self.service = service
        self.post = post
        self._now = now

    def observe(
        self,
        name: str,
        *,
        active: bool,
        scope: str | None = None,
        detail: str = "",
    ) -> Posted:
        """Record the condition's current truth; announce only a change.

        Reads the stored row first, which is what makes "once" survive a restart: a new process
        genuinely does not know whether the condition was already active, and the document
        tells it. A restart mid-condition therefore does not re-announce.
        """
        spec = BY_NAME.get(name)
        if spec is None:
            raise UnknownOpsEvent(
                f"{name!r} is not a known ops event; add it to SPECS with its sentences"
            )
        if scope is not None and not spec.scoped:
            raise ValueError(f"{name} is not a scoped event; got scope={scope!r}")
        if spec.scoped and scope is None:
            raise ValueError(f"{name} is per-scope; pass the symbol it is about")

        moment = to_utc(self._now())
        try:
            stored = self.repository.get(name, scope)
        except Exception:  # noqa: BLE001 - an unreadable register must not stop a service
            log.warning("could not read ops event %s", name, exc_info=True)
            stored = None

        was_active = bool(stored.active) if stored is not None else False
        if was_active == active:
            # No edge. The row's `detail` is still refreshed, so `/ops` shows the latest
            # reading of a condition that is still true -- but `since` is NOT touched, or an
            # onset three hours old would read as new on every poll.
            if stored is not None and detail and detail != stored.detail:
                self._store(
                    name=name,
                    scope=scope,
                    active=active,
                    since=stored.since,
                    detail=detail,
                    onsets=stored.onsets,
                    now=moment,
                )
            return Posted(name, scope, None, None)

        onsets = (stored.onsets if stored is not None else 0) + (1 if active else 0)
        self._store(
            name=name,
            scope=scope,
            active=active,
            since=moment if active else None,
            detail=detail,
            onsets=onsets,
            now=moment,
        )

        edge = "onset" if active else "recovery"
        sentence = spec.onset if active else spec.recovery
        where = f" [{scope}]" if scope else ""
        message = f"**{name}**{where} — {sentence}"
        if detail:
            message += f"\n`{detail}`"
        if active and onsets > 1:
            # A condition that flaps is a different problem from one that started once and
            # stayed, and both read as "active" without the count.
            message += f"\n(onset #{onsets} since this register was created)"
        self._announce(message)
        return Posted(name, scope, edge, message)

    def _store(
        self,
        *,
        name: str,
        scope: str | None,
        active: bool,
        since: datetime | None,
        detail: str,
        onsets: int,
        now: datetime,
    ) -> None:
        """Write the row. A failure here is logged and swallowed.

        Reporting must never stop a service: an observer that died because it could not record
        that it was slow would be a worse outcome than the slowness.
        """
        from aureon.storage import paths

        event = OpsEvent(
            event_id=paths.ops_event_id(name, scope),
            name=name,
            scope=scope,
            active=active,
            since=since,
            detail=detail,
            service=self.service,
            onsets=onsets,
        )
        try:
            self.repository.write(event, now=now)
        except Exception:  # noqa: BLE001 - reporting must never stop a service
            log.warning("could not write ops event %s", event.event_id, exc_info=True)

    def _announce(self, message: str) -> None:
        log.warning("ops: %s", message.replace("\n", " "))
        if self.post is None:
            return
        try:
            self.post(message)
        except Exception:  # noqa: BLE001 - a failed post must not stop a service
            log.exception("could not post an ops event")


def render_ops(events: list[OpsEvent], *, now: datetime | None = None) -> list[str]:
    """The ``/ops`` body: one line per condition, active first (11A, F-15).

    Cleared conditions are shown rather than omitted: "this has not happened since the
    deployment started" and "this happened twice this morning and cleared" are different things
    to know, and a list of only active rows cannot tell them apart. A condition never seen at
    all simply has no row, which is the third distinct state.
    """
    moment = to_utc(now or utc_now())
    if not events:
        return ["No operational condition has been recorded since this deployment started."]

    lines = []
    for event in events:
        age = ""
        if event.since is not None:
            minutes = (moment - to_utc(event.since)).total_seconds() / 60.0
            age = f" for {minutes:.0f}m" if minutes >= 1 else " just now"
        where = f" [{event.scope}]" if event.scope else ""
        if event.active:
            lines.append(
                f"🔴 **{event.name}**{where} active{age}"
                + (f" · {event.detail}" if event.detail else "")
                + (f" · {event.service}" if event.service else "")
            )
        else:
            seen = f" · {event.onsets}× since deploy" if event.onsets else " · never fired"
            lines.append(f"🟢 {event.name}{where} clear{seen}")
    return lines

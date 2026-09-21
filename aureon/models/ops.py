"""Named operational conditions, and whether each one is currently true (11A, F-15).

Aureon runs four processes on a box nobody is watching. When something goes wrong the
failure is almost never a crash — the supervisor handles those. It is a service that is still
running and no longer doing its job: an observer whose candle loop stopped, an outbox whose
deliveries are failing, a reconciliation that ended ambiguous. Those produce no log line
anybody reads and no alert anybody gets.

So each one is a NAMED condition with a threshold, posted **once when it starts and once when
it clears**. Not on every poll: a condition that persists for six hours would otherwise
produce six hours of identical messages, which trains an operator to mute the channel, and a
muted channel is worse than no channel.

## Why this is a stored document rather than a variable

``/ops`` runs in the Discord process. The conditions are detected in the observer, the
executor and the monitor. A register held in memory could not be read by the command that
exists to show it, so each service writes its own events here and Discord reads all of them.

The stored row is also what makes "once" survive a restart in the useful direction: a new
process genuinely does not know whether a condition was already active, and re-reading the
document tells it. A restart mid-condition therefore does not re-announce.

## What an event is NOT

It is not a log line, and it is not an audit record. ``audit_logs`` records what a human or a
service *did*; this records what the system *is*. And it never gates anything: no code path
reads an ops event to decide whether to trade. A condition that should stop execution stops it
through the guard, with a ``FailureCode``, where it is testable.
"""

from __future__ import annotations

from pydantic import ConfigDict, Field, model_validator

from aureon.models.base import AureonDocument, UtcDatetime


class OpsEvent(AureonDocument):
    """One named condition, and whether it is currently true."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(description="{name} or {name}__{scope}; see paths.ops_event_id.")
    name: str
    #: The instrument or subject a scoped event is about -- ``symbol_feed_stale`` is per
    #: symbol, because silver's feed going quiet says nothing about gold's and one event for
    #: both would clear as soon as either recovered.
    scope: str | None = None
    active: bool = False
    #: When the CURRENT state began. Not "when it was last seen": an operator reading a
    #: three-hour-old onset needs to know it has been three hours, and a field that ticked
    #: forward on every poll would always read as new.
    since: UtcDatetime | None = None
    detail: str = ""
    #: Which service wrote this. Two services can legitimately raise the same named
    #: condition -- ``firestore_unavailable`` is true for whichever process cannot write --
    #: and an operator needs to know which one is complaining.
    service: str = ""
    #: How many times this condition has started since the document was created. A condition
    #: that flaps ten times an hour is a different problem from one that started once and
    #: stayed, and both show as "active" without this.
    onsets: int = Field(default=0, ge=0)
    updated_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def _active_events_know_when_they_started(self) -> OpsEvent:
        if self.active and self.since is None:
            raise ValueError(f"{self.event_id}: an active event must carry `since`")
        return self

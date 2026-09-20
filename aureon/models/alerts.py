"""Price alerts and notification records (9C).

Two small documents that exist for the same reason: **something must be said exactly once**.

``Notification`` is the record that a message was posted. Its id is derived from what it is
about, so a bot that restarts mid-post cannot post twice: the second attempt finds the
document. Keeping it as a document rather than in memory is the whole point -- an in-process
set is empty after a restart, which is precisely when a duplicate would be sent.

``PriceAlert`` is what a human asked to be told. It carries the answer with it: when it fires,
the observer freezes a snapshot of what the market looked like at that moment, and Discord
renders the frozen copy. Rendering from live state instead would send a reminder describing a
market a few seconds later than the one that crossed the level -- a small lie that would be
impossible to notice and impossible to reconstruct.

## Why the snapshot is a plain dict

``fired_snapshot`` holds display values -- EMA, RSI, session trend, the volume summary, the
last events. A typed sub-model per field would have to be extended in lockstep with every
agent, and the rendering is a human's line of text rather than an input to a calculation. The
shape is documented here and pinned by a test, which is the same trade ``SymbolState.last_*``
already makes.
"""

from __future__ import annotations

from pydantic import ConfigDict, Field, model_validator

from aureon.models.base import AureonDocument, UtcDatetime
from aureon.models.enums import NotificationKind, NotificationStatus, PriceAlertStatus

#: Sides a level can be crossed from. ``above`` means "tell me when price reaches or exceeds
#: this level", ``below`` the reverse.
ALERT_SIDES: tuple[str, ...] = ("above", "below")

#: How long an alert stays armed without being answered. A day, because the thing a trader
#: asked about in this session stops being the thing they meant in the next one -- and an
#: alert that fires next week is noise wearing the words of a decision.
DEFAULT_ALERT_TTL_HOURS = 24.0

#: At most this many armed alerts per user. Not a resource limit: twenty levels is already
#: more than anyone is watching, and the hundredth armed alert makes the notification channel
#: useless for the ones that matter.
MAX_ARMED_ALERTS_PER_USER = 20


class Notification(AureonDocument):
    """One thing Discord said, so it cannot say it twice (9C)."""

    model_config = ConfigDict(extra="forbid")

    notification_id: str = Field(description="{kind}__{ref_id}; see paths.notification_id.")
    kind: NotificationKind
    symbol: str
    ref_id: str = Field(description="detection_id or alert_id -- what this is about.")
    channel_id: str
    sent_at: UtcDatetime | None = None
    status: NotificationStatus = NotificationStatus.SENT
    #: Why a FAILED notification failed, for the operator rather than for a retry.
    failure_message: str | None = None


class PriceAlert(AureonDocument):
    """A level a human asked to be told about (9C, §71)."""

    model_config = ConfigDict(extra="forbid")

    alert_id: str
    symbol: str
    level: float = Field(gt=0)
    side: str = Field(description=f"One of {ALERT_SIDES}.")
    requested_by: str = Field(description="Discord user id -- the only recipient.")
    note: str | None = Field(
        default=None, description="The human's own words, echoed back when it fires."
    )

    status: PriceAlertStatus = PriceAlertStatus.ARMED
    created_at: UtcDatetime | None = None
    expires_at: UtcDatetime | None = None

    fired_at: UtcDatetime | None = None
    fired_price: float | None = None
    fired_snapshot: dict[str, object] = Field(
        default_factory=dict,
        description=(
            "What the market looked like when it crossed, frozen by the observer. "
            "Rendered by Discord as-is; never recomputed (9C)."
        ),
    )
    cancelled_by: str | None = None

    @model_validator(mode="after")
    def _side_is_known(self) -> PriceAlert:
        if self.side not in ALERT_SIDES:
            raise ValueError(f"unknown alert side {self.side!r}; one of {ALERT_SIDES}")
        return self

    def crossed_by(self, bid: float, ask: float) -> bool:
        """Whether this quote answers the alert (9C).

        The side of the book a trader would transact at, not the mid: an alert to be told
        when gold reaches 3700 from below is answered when they could have **bought** at
        3700, which is the ask. Using the mid would fire half a spread early, every time, in
        the direction that flatters the alert.
        """
        if self.side == "above":
            return ask >= self.level
        return bid <= self.level

    @property
    def is_armed(self) -> bool:
        return self.status is PriceAlertStatus.ARMED

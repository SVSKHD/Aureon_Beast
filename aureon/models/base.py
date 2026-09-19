"""Base model and timestamp plumbing shared by every Aureon document.

Two CLAUDE.md non-negotiables are enforced structurally here rather than by
review:

* **Every timestamp is tz-aware.** ``AwareDatetime`` rejects a naive datetime at
  validation time, so a ``datetime.now()`` without a zone cannot reach Firestore
  through a model. Anything stored is normalised to UTC.
* **Every document carries ``schema_version``** (§6, decision 12): one integer
  for the whole system, so a later migration can branch on it without needing a
  per-collection version scheme first.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from zoneinfo import ZoneInfo

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    field_validator,
)

# Decision 12 (§6): a single integer on every document. Bump only with a
# migration note in the decisions doc.
SCHEMA_VERSION = 1


def utc_now() -> datetime:
    """The current instant, tz-aware, in UTC.

    The only sanctioned "now" in the codebase. ``datetime.now()`` without a
    zone, and any reliance on the local OS zone, are forbidden by CLAUDE.md.
    """
    return datetime.now(UTC)


def to_utc(value: datetime) -> datetime:
    """Normalise a tz-aware datetime to UTC, rejecting naive input."""
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(
            "naive datetime is not allowed; every Aureon timestamp must be tz-aware"
        )
    return value.astimezone(UTC)


# A UTC-normalising aware datetime. Serialises to RFC 3339 with an explicit
# offset so a stored value can never be read back as ambiguous local time.
UtcDatetime = Annotated[
    AwareDatetime,
    PlainSerializer(lambda v: to_utc(v).isoformat(), return_type=str, when_used="json"),
]


class AureonModel(BaseModel):
    """Strict base for every model in the system.

    ``extra="forbid"`` is deliberate: a typo'd field name is a bug we want at
    validation time, not a silently-ignored key that makes a document look
    complete while a value never lands.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=False,
        validate_assignment=True,
        populate_by_name=True,
        ser_json_timedelta="float",
    )


class AureonDocument(AureonModel):
    """A model that is stored as a Firestore document."""

    schema_version: int = Field(
        default=SCHEMA_VERSION,
        description="Document schema version (§6, decision 12).",
    )


class MarketTime(AureonModel):
    """An instant, stored as UTC, renderable in the broker's market zone.

    CLAUDE.md requires stored records to use ``MarketTime`` rather than a bare
    datetime. The reason is that Aureon reasons in two clocks at once: UTC is
    the only safe storage and comparison key, while sessions, day boundaries and
    "previous day high" are all defined in the *broker's* zone (§8). Keeping
    both in one value object means a reader never has to guess which clock a
    timestamp is in, and the market rendering is always derived -- never stored
    and never allowed to drift out of step with ``utc``.
    """

    utc: UtcDatetime = Field(description="The instant, always normalised to UTC.")
    market_tz: str = Field(description="IANA zone of the broker's server clock (§8).")

    @field_validator("utc")
    @classmethod
    def _normalise_to_utc(cls, value: datetime) -> datetime:
        return to_utc(value)

    @field_validator("market_tz")
    @classmethod
    def _zone_must_exist(cls, value: str) -> str:
        # Fail on a typo'd zone here rather than at the first session boundary.
        ZoneInfo(value)
        return value

    @property
    def market(self) -> datetime:
        """The same instant rendered in the broker's zone (DST-aware)."""
        return self.utc.astimezone(ZoneInfo(self.market_tz))

    @property
    def market_date(self) -> str:
        """The broker-local calendar date, ``YYYY-MM-DD``.

        This is the correct key for "trades today" and for the daily review: the
        broker's trading day, not the UTC day, and not the observer host's day.
        """
        return self.market.date().isoformat()

    @classmethod
    def from_utc(cls, value: datetime, market_tz: str) -> MarketTime:
        return cls(utc=value, market_tz=market_tz)

    @classmethod
    def now(cls, market_tz: str) -> MarketTime:
        return cls(utc=utc_now(), market_tz=market_tz)

    def __str__(self) -> str:  # pragma: no cover - diagnostics only
        return f"{self.utc.isoformat()} ({self.market.isoformat()} {self.market_tz})"

"""The declarative base and the column types every table shares (plan §5, §7).

The tables themselves land with S-2's ``0001_initial``. What is here is the vocabulary
they are written in, and two of the three entries are guards rather than conveniences.

**Persistence models are not domain models.** Plan §5: the pydantic models in
``aureon/models`` stay the contract the rest of the system speaks, and these are the rows
underneath. Keeping them separate costs a mapping in each repository and buys the thing
that matters -- a column can be indexed, widened or split without changing what a
detection *is*, and a domain model cannot acquire a database concern by accident.

**Column rule (plan §7).** Relational columns for anything filtered, ordered, identified,
claimed or transitioned on; ``JSONB`` for frozen context that is read back whole and never
queried into. The distinction is not about size -- it is about whether a query will ever
need to reach inside. A market snapshot is read as a unit and is JSONB; a ``market_date``
is filtered by every review and is a column.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.types import TypeDecorator

from aureon.models.base import to_utc

#: Frozen context: market snapshots, MTF context, setup reference blocks, metadata,
#: research snapshots. Re-exported here so a table declares ``Json`` and no repository has
#: to know that the dialect-specific type lives in a SQLAlchemy submodule.
Json = JSONB


class NaiveTimestamp(ValueError):
    """A timestamp without a timezone tried to cross the storage boundary.

    Its own type because the two directions mean different things and only one of them is
    the caller's fault. See ``UtcTimestamp``.
    """


class UtcTimestamp(TypeDecorator):
    """``timestamptz``, with the tz-aware rule enforced in both directions.

    CLAUDE.md: *"Every timestamp is tz-aware. Never ``datetime.now()`` without tz; never
    local OS time."* Until now that rule lived in the pydantic models, which means it held
    for anything that went through a model and not for anything that did not. A repository
    building an ``UPDATE`` from a plain ``datetime`` bypassed it entirely.

    **On the way in**, a naive value is refused. Not coerced: a naive datetime is a value
    whose meaning nobody recorded, and assuming UTC would silently shift a European
    machine's timestamps by two or three hours -- in the direction that makes a detection
    look like it was found before the candle that produced it closed.

    **On the way out**, a naive value is also refused, and that one is a *schema* error
    rather than a caller error: psycopg returns an aware datetime for every
    ``timestamptz``, so a naive result means the column was declared ``timestamp`` without
    a zone. That mistake is invisible -- every read succeeds, every value is wrong by the
    server's offset -- so it is worth failing on the first row rather than discovering it
    in a review three weeks later.

    **What a caller catches.** SQLAlchemy wraps anything a bind processor raises, so the
    write path surfaces ``sqlalchemy.exc.StatementError`` with the ``NaiveTimestamp`` on
    its ``.orig``; the read path raises ``NaiveTimestamp`` directly. Documented because a
    caller reading only the ``raise`` statements below would write an ``except`` that never
    fires (decision 333). Either way the statement does not execute and nothing is written.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, datetime):
            # An ISO STRING, most likely -- the outbox stores JSON payloads and a repository
            # that passed one straight through would otherwise get
            # ``AttributeError: 'str' object has no attribute 'tzinfo'`` from inside
            # SQLAlchemy's bind processing, which names neither the column nor the cause.
            #
            # Refused rather than parsed: an ISO string without an offset is exactly the
            # naive value the rest of this class exists to reject, and accepting the ones
            # that happen to carry an offset would make the guard depend on how the caller
            # happened to serialise (decision 345).
            raise NaiveTimestamp(
                f"expected a tz-aware datetime, got {type(value).__name__} {value!r}. "
                "Parse it first; a string cannot be checked for a timezone."
            )
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise NaiveTimestamp(
                f"refusing to store the naive timestamp {value!r}. Every Aureon timestamp "
                "is tz-aware (CLAUDE.md); use MarketTime or an explicit tzinfo."
            )
        return to_utc(value)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise NaiveTimestamp(
                f"the database returned the naive timestamp {value!r}. The column is "
                "declared without a time zone; it must be timestamptz."
            )
        return value.astimezone(UTC)


class Base(DeclarativeBase):
    """The declarative base every Aureon table derives from.

    Also the input to two generated artefacts, which is why there is exactly one:
    ``docs/CONTRACTS.md`` is regenerated from ``Base.metadata`` (S-2) and the ownership
    table in ``docs/ARCHITECTURE.md`` from the same place (S-11). A second base would give
    both generators a partial view and neither would say so.
    """

    type_annotation_map = {
        # So a mapped ``datetime`` gets the guard above without every column repeating it.
        # A column that opted out by writing ``DateTime`` directly would be the one place
        # a naive value could get in, so the default is the strict type.
        datetime: UtcTimestamp,
        dict[str, Any]: Json,
    }


__all__ = ["Base", "Json", "NaiveTimestamp", "UtcTimestamp"]

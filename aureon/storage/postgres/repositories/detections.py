"""Detections, on PostgreSQL (§19, §83, plan §8).

Immutable. There is no update method and there never will be: an outcome belongs in
``detection_evaluations`` (§21), and a field on the detection that encoded what happened
next would make every historical record un-trustable, because nothing could say whether a
given row was written at the candle or edited afterwards.

The write is an upsert at ``detection_id``, which is a pure function of the candle (§12).
That is what lets the outbox retry an ambiguous delivery: if the first attempt landed, the
retry overwrites it with identical content.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select

from aureon.models.base import to_utc
from aureon.models.detection import Detection
from aureon.storage.postgres import tables
from aureon.storage.postgres.repositories.base import PostgresRepository


def market_date_of(moment: datetime, market_tz: str) -> str:
    """The BROKER day a UTC instant falls in (§8).

    Derived here and stored as a column rather than computed on every read, because §8
    indexes ``(symbol, market_date)`` and every review filters on it -- and because the
    derivation needs the zone, which a SQL expression would have to hard-code.
    """
    from zoneinfo import ZoneInfo

    return to_utc(moment).astimezone(ZoneInfo(market_tz)).date().isoformat()


class DetectionRepository(PostgresRepository):
    """Reads and upserts ``detections``."""

    # The ORM class rather than a metadata lookup by name: a typo here is an ImportError
    # at import time instead of a KeyError at first use, and it means no repository ever
    # contains a bare table-name literal -- which keeps them all inside the §83 guard.
    table = tables.Detection.__table__

    # ── Writing ───────────────────────────────────────────────────────────────

    def upsert(self, detection: Detection, *, connection: Any = None) -> str:
        """Write a detection at its deterministic id. Safe to repeat."""
        self._upsert(self._to_row(detection), connection=connection)
        return detection.detection_id

    def upsert_payload(self, payload: dict[str, Any], *, connection: Any = None) -> str:
        """Upsert an already-serialised detection.

        The outbox stores payloads rather than models, so a detection queued by an earlier
        version of the code can be delivered without that payload having to survive a
        model round-trip first. It is validated here anyway: a payload that no longer
        parses is a payload whose columns we cannot fill, and writing a partial row would
        be worse than failing the delivery.
        """
        detection = Detection.model_validate(payload)
        return self.upsert(detection, connection=connection)

    # ── Reading ───────────────────────────────────────────────────────────────

    def get(self, detection_id: str) -> Detection | None:
        row = self._row(detection_id)
        return None if row is None else Detection.model_validate(self._to_model_dict(row))

    def recent_for_symbol(
        self, symbol: str, *, since: datetime | None = None, limit: int = 10
    ) -> list[Detection]:
        """Most recent detections for a symbol, newest first (§37).

        Backs the Discord detection selector. Ordered by ``candle_close_utc`` -- the
        instant the detection became knowable -- rather than by insertion, so a backfill
        cannot reorder history.
        """
        statement = select(self.table).where(self.table.c.symbol == symbol)
        if since is not None:
            statement = statement.where(self.table.c.candle_close_utc >= to_utc(since))
        statement = statement.order_by(self.table.c.candle_close_utc.desc()).limit(limit)
        return self._parse_all(self._rows(statement), Detection, what="detection")

    def in_period(self, start: datetime, end: datetime) -> list[Detection]:
        """Detections whose candle closed in ``[start, end)``.

        A real query with a real index, which is the first thing this migration buys. The
        Firestore version streamed the whole collection and filtered in Python, because a
        composite query there needed a declared index kept in step and a missing one
        returns FEWER documents with no error -- a quietly wrong denominator in a review.
        Here the index is in ``0001_initial`` and a missing one is a slow query, not a
        wrong answer.
        """
        statement = (
            select(self.table)
            .where(self.table.c.candle_close_utc >= to_utc(start))
            .where(self.table.c.candle_close_utc < to_utc(end))
            .order_by(self.table.c.candle_close_utc)
        )
        return self._parse_all(self._rows(statement), Detection, what="detection")

    def for_market_date(self, symbol: str, market_date: str) -> list[Detection]:
        """One broker day's detections for one symbol (§8's index).

        The question a daily review asks. Kept separate from ``in_period`` because a
        broker day is not a UTC day and converting one to a UTC range at the call site is
        how a review silently loses the hour either side of the boundary.
        """
        statement = (
            select(self.table)
            .where(self.table.c.symbol == symbol)
            .where(self.table.c.market_date == market_date)
            .order_by(self.table.c.candle_close_utc)
        )
        return self._parse_all(self._rows(statement), Detection, what="detection")

    # ── Mapping ───────────────────────────────────────────────────────────────

    @staticmethod
    def _to_row(detection: Detection) -> dict[str, Any]:
        """Domain model to row.

        ``detected_at`` is the candle CLOSE (§12) and becomes ``candle_close_utc``; the
        zone travels once, in ``timezone``, because both MarketTimes on a detection carry
        the same one and storing it twice would let them disagree.
        """
        payload = detection.model_dump(mode="json")
        return {
            "detection_id": detection.detection_id,
            "schema_version": detection.schema_version,
            "account_scope": detection.account_scope,
            "symbol": detection.symbol,
            "timeframe": detection.timeframe.value,
            "agent_name": detection.agent_name,
            "agent_version": detection.agent_version,
            "event_key": detection.event_key,
            "direction": detection.direction.value if detection.direction else None,
            "candle_close_utc": detection.detected_at.utc,
            "candle_time_utc": detection.candle_open_time.utc,
            "timezone": detection.detected_at.market_tz,
            "market_date": market_date_of(
                detection.detected_at.utc, detection.detected_at.market_tz
            ),
            "price": detection.price,
            "sequence_today": detection.sequence_today,
            "sequence_session": detection.sequence_session,
            "session": detection.session.session.value,
            "session_config_version": str(detection.session.session_config_version),
            "agent_params_snapshot": payload["agent_params_snapshot"],
            "indicators": payload.get("indicators"),
            "levels": payload["levels"],
            "evidence": payload["evidence"],
            "volume_profile_ref": payload.get("volume_profile_ref"),
            "volatility": payload.get("volatility"),
            "mtf": payload.get("mtf"),
        }

    @staticmethod
    def _to_model_dict(row: Any) -> dict[str, Any]:
        """Row to domain model.

        ``market_date`` is dropped rather than passed through: it is derived from
        ``candle_close_utc`` and the zone, the model has no field for it, and
        ``extra="forbid"`` would reject it. That is the right behaviour -- a column the
        model does not know about should fail loudly here rather than be silently ignored.
        """
        data = dict(row)
        return {
            "schema_version": data["schema_version"],
            "detection_id": data["detection_id"],
            "account_scope": data["account_scope"],
            "symbol": data["symbol"],
            "timeframe": data["timeframe"],
            "agent_name": data["agent_name"],
            "agent_version": data["agent_version"],
            "agent_params_snapshot": data["agent_params_snapshot"],
            "event_key": data["event_key"],
            "direction": data["direction"],
            "detected_at": {
                "utc": data["candle_close_utc"],
                "market_tz": data["timezone"],
            },
            "candle_open_time": {
                "utc": data["candle_time_utc"],
                "market_tz": data["timezone"],
            },
            "price": data["price"],
            "indicators": data["indicators"],
            "session": {
                "session": data["session"],
                "session_config_version": data["session_config_version"],
            },
            "levels": data["levels"],
            "evidence": data.get("evidence") or {},
            "sequence_today": data["sequence_today"],
            "sequence_session": data["sequence_session"],
            "volume_profile_ref": data["volume_profile_ref"],
            "volatility": data["volatility"],
            "mtf": data["mtf"],
        }

"""Persistence for V3 EMA movement journeys."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from aureon.models.ema_journey_v3 import EMAMovementJourney
from aureon.storage.postgres import tables
from aureon.storage.postgres.repositories.base import PostgresRepository


class EMAMovementJourneyRepository(PostgresRepository):
    table = tables.EMAMovementJourney.__table__

    def upsert(self, journey: EMAMovementJourney) -> str:
        payload = journey.model_dump(mode="json")
        self._upsert(
            {
                "journey_id": journey.journey_id,
                "schema_version": journey.schema_version,
                "account_scope": journey.account_scope,
                "symbol": journey.symbol,
                "timeframe": journey.timeframe.value,
                "direction": journey.direction.value,
                "market_date": journey.market_date,
                "status": journey.status.value,
                "started_at": journey.started_at,
                "ended_at": journey.ended_at,
                "end_reason": journey.end_reason.value if journey.end_reason else None,
                "payload": payload,
            }
        )
        return journey.journey_id

    def get(self, journey_id: str) -> EMAMovementJourney | None:
        row = self._row(journey_id)
        if row is None:
            return None
        return EMAMovementJourney.model_validate(dict(row)["payload"])

    def open_for_symbol(self, symbol: str) -> list[EMAMovementJourney]:
        statement = (
            select(self.table)
            .where(self.table.c.symbol == symbol)
            .where(self.table.c.status == "open")
            .order_by(self.table.c.started_at)
        )
        return [
            EMAMovementJourney.model_validate(dict(row)["payload"])
            for row in self._rows(statement)
        ]

    def for_detection(self, detection_id: str) -> EMAMovementJourney | None:
        """Find the journey containing one EMA anchor detection."""
        statement = select(self.table).order_by(self.table.c.started_at.desc())
        for row in self._rows(statement):
            journey = EMAMovementJourney.model_validate(dict(row)["payload"])
            if any(anchor.detection_id == detection_id for anchor in journey.anchors):
                return journey
        return None

    def closed_since(self, since: datetime) -> list[EMAMovementJourney]:
        statement = (
            select(self.table)
            .where(self.table.c.status.in_(("closed", "invalid")))
            .where(self.table.c.ended_at.is_not(None))
            .where(self.table.c.ended_at >= since)
            .order_by(self.table.c.ended_at)
        )
        return [
            EMAMovementJourney.model_validate(dict(row)["payload"])
            for row in self._rows(statement)
        ]

    def latest_for_symbol(self, symbol: str) -> EMAMovementJourney | None:
        statement = (
            select(self.table)
            .where(self.table.c.symbol == symbol.upper())
            .order_by(self.table.c.started_at.desc())
            .limit(1)
        )
        rows = self._rows(statement)
        return None if not rows else EMAMovementJourney.model_validate(dict(rows[0])["payload"])

    def for_market_date(self, symbol: str, market_date: str) -> list[EMAMovementJourney]:
        statement = (
            select(self.table)
            .where(self.table.c.symbol == symbol)
            .where(self.table.c.market_date == market_date)
            .order_by(self.table.c.started_at)
        )
        return [
            EMAMovementJourney.model_validate(dict(row)["payload"])
            for row in self._rows(statement)
        ]

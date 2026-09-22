"""Daily and weekly reviews, on PostgreSQL (§37, §61-§63).

Keyed so **regenerating a period overwrites the same row**: a daily review by its broker
date and symbol, a weekly one by ISO year and week. A period has one answer, and if the
answer changed then the old one was wrong rather than also true.

**The read side is a separate class with no write method, and that is a boundary rather
than tidiness.** ``/status`` shows the latest completed review, so Discord must READ one --
but a review is an observation of history, and a human interface holding an object that can
rewrite it makes every figure built on those reviews unfalsifiable. The split survives the
migration unchanged: ``ReviewReader`` is what Discord imports, ``ReviewRepository`` is what
the review service imports, and the boundary test keeps the writer out of the Discord
package.

**"Latest" is now an ``ORDER BY``, not a maximum over every id.** The Firestore reader
streamed the whole collection and took ``max(doc.id)``, which was correct only because the
ids sort chronologically as strings (decision 26) and cheap only because the collections
are small. The ids keep that shape -- the export has to land on them -- but the read is an
indexed descending order with ``LIMIT 1``, so it stays one row regardless of how many years
of reviews accumulate.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from aureon.models.review import DailyReview, WeeklyReview
from aureon.storage.postgres import tables
from aureon.storage.postgres.repositories.base import PostgresRepository


def daily_review_row_id(market_date: str, symbol: str | None = None) -> str:
    """``{market_date}`` or ``{market_date}_{SYMBOL}`` (9A), unchanged from Firestore.

    The symbol is a SUFFIX so the ids still sort chronologically as strings, which is what
    makes "the latest review" an ordered read rather than a scan over parsed dates.
    """
    if not market_date:
        raise ValueError("market_date must not be empty")
    return f"{market_date}_{symbol.upper()}" if symbol else market_date


def weekly_review_row_id(iso_year: int, iso_week: int, symbol: str | None = None) -> str:
    """``{iso_year}-W{iso_week}`` or ``{iso_year}-W{iso_week}_{SYMBOL}``."""
    week = f"{iso_year:04d}-W{iso_week:02d}"
    return f"{week}_{symbol.upper()}" if symbol else week


class ReviewReader(PostgresRepository):
    """Reads ``daily_reviews`` and ``weekly_reviews``. Cannot write them.

    Holds the daily table so the inherited primary-key helpers work on it; the weekly reads
    name their table explicitly. A reader with two tables is the shape Discord wants -- one
    object answering "what is the latest review" without also being able to replace it.
    """

    table = tables.DailyReview.__table__

    weekly = tables.WeeklyReview.__table__

    def get_daily(self, market_date: str, symbol: str | None = None) -> DailyReview | None:
        row = self._row(daily_review_row_id(market_date, symbol))
        return None if row is None else DailyReview.model_validate(row["review"])

    def get_weekly(
        self, iso_year: int, iso_week: int, symbol: str | None = None
    ) -> WeeklyReview | None:
        row = self._row(
            weekly_review_row_id(iso_year, iso_week, symbol), table=self.weekly
        )
        return None if row is None else WeeklyReview.model_validate(row["review"])

    def latest_daily(self, symbol: str | None = None) -> DailyReview | None:
        return self._latest(self.table, DailyReview, symbol)

    def latest_weekly(self, symbol: str | None = None) -> WeeklyReview | None:
        return self._latest(self.weekly, WeeklyReview, symbol)

    def latest_for(self, symbol: str | None = None) -> Any | None:
        """The weekly review if there is one, else the daily -- for one symbol.

        The preference ``/status`` has always had: on a weekend the most recent daily covers
        Friday alone, while the weekly is the wider picture.
        """
        return self.latest_weekly(symbol) or self.latest_daily(symbol)

    def _latest(self, table: Any, model: type, symbol: str | None) -> Any | None:
        statement = select(table).order_by(table.c.id.desc()).limit(1)
        if symbol is not None:
            # The stored COLUMN, not the id: an id is a naming convention, and the column is
            # the row's own statement about what it measured. Narrowing first matters --
            # without it the maximum id is whichever symbol happens to sort last, which is
            # not an answer to any question.
            statement = select(table).where(table.c.symbol == symbol.upper())
            statement = statement.order_by(table.c.id.desc()).limit(1)
        rows = self._rows(statement)
        return None if not rows else model.model_validate(rows[0]["review"])


class ReviewRepository(PostgresRepository):
    """Reads and upserts ``daily_reviews`` and ``weekly_reviews``."""

    table = tables.DailyReview.__table__

    weekly = tables.WeeklyReview.__table__

    def __init__(self, database: Any) -> None:
        super().__init__(database)
        # Delegated rather than duplicated, so there is one implementation of "which review
        # is the latest" and the two cannot answer differently.
        self.reader = ReviewReader(database)

    def upsert_daily(self, review: DailyReview) -> str:
        row_id = daily_review_row_id(review.market_date, review.symbol)
        self._upsert(
            {
                "id": row_id,
                "schema_version": review.schema_version,
                "market_date": review.market_date,
                "symbol": review.symbol,
                "generated_at": review.generated_at,
                "evaluation_rule_id": review.evaluation_rule_id,
                "review": review.model_dump(mode="json"),
            }
        )
        return row_id

    def upsert_weekly(self, review: WeeklyReview) -> str:
        row_id = weekly_review_row_id(review.iso_year, review.iso_week, review.symbol)
        self._upsert(
            {
                "id": row_id,
                "schema_version": review.schema_version,
                "iso_year": review.iso_year,
                "iso_week": review.iso_week,
                "symbol": review.symbol,
                "generated_at": review.generated_at,
                "evaluation_rule_id": review.evaluation_rule_id,
                "review": review.model_dump(mode="json"),
            },
            table=self.weekly,
        )
        return row_id

    def get_daily(self, market_date: str, symbol: str | None = None) -> DailyReview | None:
        return self.reader.get_daily(market_date, symbol)

    def get_weekly(
        self, iso_year: int, iso_week: int, symbol: str | None = None
    ) -> WeeklyReview | None:
        return self.reader.get_weekly(iso_year, iso_week, symbol)

    def latest_daily(self, symbol: str | None = None) -> DailyReview | None:
        return self.reader.latest_daily(symbol)

    def latest_weekly(self, symbol: str | None = None) -> WeeklyReview | None:
        return self.reader.latest_weekly(symbol)

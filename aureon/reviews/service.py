"""Generating reviews from Firestore (§61-§65).

Loads a period's detections, evaluations, trades and sessions, aggregates them, and writes
one document. Idempotent: the same period regenerates the same document id with
byte-identical content.

## Why the loading is deliberately simple

Detections and trades are fetched by a **single-field range**, then filtered in Python.
That avoids composite Firestore indexes -- which would have to be declared, deployed and
kept in step for a batch job that runs a few times a day and reads at most a few thousand
documents. Evaluations are fetched by exact document id, which needs no index at all and
cannot miss one.

Reviews never write to ``detections``, ``trades`` or ``trade_requests``. They read those
and write only their own document (§50): an inferred link is analysis, and analysis must
not edit the record it analyses.

## One review per symbol (9A)

A service is constructed for **one** symbol and one rule, and filters the period to that
symbol. Aggregating both would produce a document whose ``evaluation_rule_id`` is a false
statement about half its numbers -- each symbol has its own frozen rule (decision 141) -- and
whose horizon table would add reached-counts measured against thresholds denominated in
different instruments' money. The shape is identical, so a reader compares two documents
rather than reading one that quietly mixes two things.

``symbol=None`` keeps the pre-9A behaviour: everything in the period, one document, no symbol
on it. That is what a single-symbol deployment has always produced.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from aureon.models.base import to_utc
from aureon.models.detection import Detection
from aureon.models.evaluation import DetectionEvaluation, EvaluationRule
from aureon.models.review import DailyReview, WeeklyReview
from aureon.models.session import SessionSummary
from aureon.models.trade import Trade
from aureon.reviews.aggregate import (
    PeriodData,
    build_daily_review,
    build_weekly_review,
    excluded_summary,
)
from aureon.reviews.periods import Period, day_period, week_period
from aureon.storage.period_reader import PeriodReader
from aureon.storage.review_repository import ReviewRepository

log = logging.getLogger(__name__)


class ReviewService:
    """Builds and stores daily and weekly reviews."""

    def __init__(
        self,
        client: Any,
        rule: EvaluationRule,
        *,
        market_tz: str,
        infer_window_minutes: int,
        account_scope: str = "primary",
        symbol: str | None = None,
    ) -> None:
        self._client = client
        self.rule = rule
        #: Which symbol this service reviews, or ``None`` for everything in the period.
        self.symbol = symbol.upper() if symbol else None
        self.market_tz = market_tz
        self.infer_window_minutes = infer_window_minutes
        self.account_scope = account_scope
        if hasattr(client, "period_reader") and hasattr(client, "reviews"):
            # Local runtime bundle: repository capabilities are already composed and no
            # cloud client exists.
            self.reviews = client.reviews
            self.source = client.period_reader
        else:
            # Kept for isolated legacy unit doubles; production runtime never takes this path.
            self.reviews = ReviewRepository(client)
            self.source = PeriodReader(client, account_scope=account_scope)

    # ── Loading ───────────────────────────────────────────────────────────────

    def load(self, period: Period) -> PeriodData:
        """Everything the review needs for one period, for this symbol."""
        detections = self._for_symbol(self._load_detections(period))
        data = PeriodData(
            detections=detections,
            evaluations=self._load_evaluations(detections),
            trades=self._for_symbol(self._load_trades(period)),
            sessions=self._for_symbol(self._load_sessions(period)),
            # 9D. Assessments are narrowed by symbol like everything else; notes are keyed
            # by the trades already narrowed above, so they need no second filter.
            assessments=self._for_symbol(self._load_assessments(period)),
            notes=self._load_notes(self._for_symbol(self._load_trades(period))),
            **self._load_setups(period),
        )
        log.info(
            "period %s (%s): %d detections, %d evaluations, %d trades, %d sessions (%s)",
            period.market_date,
            self.symbol or "all symbols",
            len(data.detections),
            len(data.evaluations),
            len(data.trades),
            len(data.sessions),
            excluded_summary(data),
        )
        return data

    def _for_symbol(self, records: list[Any]) -> list[Any]:
        """Narrow a period's records to this service's symbol.

        In Python rather than in the query, for the reason the module docstring gives about
        composite indexes: these are single-field range reads over at most a few thousand
        documents, and a second filter would need an index declared and deployed for a batch
        job that runs a few times a day.
        """
        if self.symbol is None:
            return records
        return [r for r in records if str(getattr(r, "symbol", "")).upper() == self.symbol]

    def _load_detections(self, period: Period) -> list[Detection]:
        return self.source.detections_in(period.start, period.end)

    def _load_evaluations(
        self, detections: list[Detection]
    ) -> dict[str, DetectionEvaluation]:
        """Fetch by exact document id.

        No index, and no chance of a query silently missing one -- which for an
        evaluation would mean its detection counted as unevaluated rather than as
        answered.
        """
        return self.source.evaluations_for(detections, self.rule.rule_id)

    def _load_trades(self, period: Period) -> list[Trade]:
        return self.source.trades_opened_in(period.start, period.end)

    def _load_sessions(self, period: Period) -> list[SessionSummary]:
        return self.source.sessions_in(period.start, period.end)

    def _load_assessments(self, period: Period) -> list[Any]:
        return self.source.assessments_in(period.start, period.end)

    def _load_notes(self, trades: list[Trade]) -> dict[str, list[Any]]:
        return self.source.notes_for(trades)

    def _load_setups(self, period: Period) -> dict[str, Any]:
        """The period's setups and their outcomes (T-9).

        Loaded for every review although only the weekly one counts them, because ``load`` is
        the one place that knows the period and narrowing it by review kind would put the
        weekly/daily distinction in two places. The cost is one symbol-scoped read per daily
        review over a collection holding tens of documents a day.

        Tolerant of a missing collection: a repository that has never written a setup returns
        nothing, and a review of a period from before T-9 must build exactly as it did before.
        """
        try:
            setups = self._for_symbol(self.source.setups_in(period.start, period.end))
        except Exception:  # noqa: BLE001 - a review must not fail on an absent collection
            log.warning("could not read setups for %s", period.market_date, exc_info=True)
            return {}
        try:
            evaluations = self.source.setup_evaluations_for(setups, self.rule.rule_id)
        except Exception:  # noqa: BLE001
            log.warning(
                "could not read setup evaluations for %s", period.market_date, exc_info=True
            )
            evaluations = {}
        return {"setups": setups, "setup_evaluations": evaluations}

    # ── Generating ────────────────────────────────────────────────────────────

    def generate_daily(
        self, market_date: str, *, generated_at: datetime | None = None, store: bool = True
    ) -> DailyReview:
        """Build (and by default store) one broker day's review (§61)."""
        period = day_period(market_date, self.market_tz)
        data = self.load(period)
        review = build_daily_review(
            data,
            self.rule,
            market_date=period.market_date,
            period_start=period.start,
            period_end=period.end,
            market_tz=self.market_tz,
            infer_window_minutes=self.infer_window_minutes,
            symbol=self.symbol,
            generated_at=to_utc(generated_at) if generated_at else None,
        )
        if store:
            self.reviews.upsert_daily(review)
            log.info("wrote daily review %s %s", review.market_date, self.symbol or "")
        return review

    def generate_weekly(
        self,
        iso_year: int,
        iso_week: int,
        *,
        generated_at: datetime | None = None,
        store: bool = True,
        include_daily_ids: bool = True,
    ) -> WeeklyReview:
        """Build (and by default store) one trading week's review (§63)."""
        period = week_period(iso_year, iso_week, self.market_tz)
        data = self.load(period)

        daily_ids: tuple[str, ...] = ()
        if include_daily_ids:
            # Only days that actually have a stored review, so the list is a reference
            # rather than a claim that seven reviews exist.
            daily_ids = tuple(
                sorted(
                    d
                    for d in _dates_in(period)
                    if self.reviews.get_daily(d, self.symbol) is not None
                )
            )

        review = build_weekly_review(
            data,
            self.rule,
            iso_year=iso_year,
            iso_week=iso_week,
            period_start=period.start,
            period_end=period.end,
            market_tz=self.market_tz,
            infer_window_minutes=self.infer_window_minutes,
            daily_review_ids=daily_ids,
            symbol=self.symbol,
            generated_at=to_utc(generated_at) if generated_at else None,
        )
        if store:
            self.reviews.upsert_weekly(review)
            log.info(
                "wrote weekly review %s-W%02d %s", iso_year, iso_week, self.symbol or ""
            )
        return review

    def generate_week_with_dailies(
        self, iso_year: int, iso_week: int, *, generated_at: datetime | None = None
    ) -> tuple[WeeklyReview, list[DailyReview]]:
        """Generate every day of a week, then the week itself.

        In that order, so the weekly review's ``daily_review_ids`` can reference documents
        that exist rather than ones it hopes will.
        """
        period = week_period(iso_year, iso_week, self.market_tz)
        dailies = [
            self.generate_daily(market_date, generated_at=generated_at)
            for market_date in _dates_in(period)
        ]
        weekly = self.generate_weekly(iso_year, iso_week, generated_at=generated_at)
        return weekly, dailies


def _dates_in(period: Period) -> list[str]:
    """Every broker date the period spans."""
    from datetime import date, timedelta

    start = date.fromisoformat(period.market_date)
    days = max(1, round((period.end - period.start).total_seconds() / 86400))
    return [(start + timedelta(days=offset)).isoformat() for offset in range(days)]

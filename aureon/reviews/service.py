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
    ) -> None:
        self._client = client
        self.rule = rule
        self.market_tz = market_tz
        self.infer_window_minutes = infer_window_minutes
        self.account_scope = account_scope
        self.reviews = ReviewRepository(client)
        # Every read goes through a repository (CLAUDE.md, decision 107). The service
        # used to stream collections off the client directly; a boundary test now fails
        # on any .collection( or .document( outside aureon/storage.
        self.source = PeriodReader(client, account_scope=account_scope)

    # ── Loading ───────────────────────────────────────────────────────────────

    def load(self, period: Period) -> PeriodData:
        """Everything the review needs for one period."""
        detections = self._load_detections(period)
        data = PeriodData(
            detections=detections,
            evaluations=self._load_evaluations(detections),
            trades=self._load_trades(period),
            sessions=self._load_sessions(period),
        )
        log.info(
            "period %s: %d detections, %d evaluations, %d trades, %d sessions (%s)",
            period.market_date,
            len(data.detections),
            len(data.evaluations),
            len(data.trades),
            len(data.sessions),
            excluded_summary(data),
        )
        return data

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
            generated_at=to_utc(generated_at) if generated_at else None,
        )
        if store:
            self.reviews.upsert_daily(review)
            log.info("wrote daily review %s", review.market_date)
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
                    if self.reviews.get_daily(d) is not None
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
            generated_at=to_utc(generated_at) if generated_at else None,
        )
        if store:
            self.reviews.upsert_weekly(review)
            log.info("wrote weekly review %s-W%02d", iso_year, iso_week)
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

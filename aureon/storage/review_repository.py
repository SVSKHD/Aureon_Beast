"""Review persistence (§61, §63).

Keyed so **regenerating a period overwrites the same document**: a daily review by its
broker date, a weekly one by ISO year and week. Re-running a review must be safe and must
not accumulate variants -- a period has one answer, and if the answer changes the old one
was wrong rather than also true.
"""

from __future__ import annotations

from typing import Any

from aureon.models.review import DailyReview, WeeklyReview
from aureon.storage import paths
from aureon.storage.review_reader import ReviewReader


class ReviewRepository:
    """Reads and upserts ``daily_reviews`` and ``weekly_reviews``."""

    def __init__(self, client: Any) -> None:
        self._client = client

    # ── Daily ─────────────────────────────────────────────────────────────────

    def upsert_daily(self, review: DailyReview) -> str:
        path = paths.daily_review_path(review.market_date)
        self._client.document(path).set(review.model_dump(mode="json"))
        return path

    def get_daily(self, market_date: str) -> DailyReview | None:
        snapshot = self._client.document(paths.daily_review_path(market_date)).get()
        if not getattr(snapshot, "exists", False):
            return None
        return DailyReview.model_validate(snapshot.to_dict())

    # ── Weekly ────────────────────────────────────────────────────────────────

    def upsert_weekly(self, review: WeeklyReview) -> str:
        path = paths.weekly_review_path(review.iso_year, review.iso_week)
        self._client.document(path).set(review.model_dump(mode="json"))
        return path

    def get_weekly(self, iso_year: int, iso_week: int) -> WeeklyReview | None:
        snapshot = self._client.document(
            paths.weekly_review_path(iso_year, iso_week)
        ).get()
        if not getattr(snapshot, "exists", False):
            return None
        return WeeklyReview.model_validate(snapshot.to_dict())

    # ── Reading ───────────────────────────────────────────────────────────────

    # The read side lives in ReviewReader, which Discord holds instead of this class
    # (see that module). Delegating rather than duplicating keeps one implementation of
    # "which review is the latest".

    def latest_weekly(self) -> WeeklyReview | None:
        return ReviewReader(self._client).latest_weekly()

    def latest_daily(self) -> DailyReview | None:
        return ReviewReader(self._client).latest_daily()

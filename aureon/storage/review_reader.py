"""Read-only access to reviews (§61-§63).

Exists because of a boundary, not for tidiness. ``/status`` must show the latest completed
review, so Discord needs to *read* one -- but ``ReviewRepository`` can also overwrite one,
and a review is an observation of history. A human interface holding an object that can
rewrite it would make every figure built on those reviews unfalsifiable.

So Discord imports this, which has no write method at all, and the boundary test keeps the
writing repository out of the Discord package. The capability split is visible in the import
graph rather than resting on nobody calling the wrong method.
"""

from __future__ import annotations

from typing import Any

from aureon.models.review import DailyReview, WeeklyReview
from aureon.storage import paths


class ReviewReader:
    """Reads ``daily_reviews`` and ``weekly_reviews``. Cannot write them."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def get_daily(self, market_date: str) -> DailyReview | None:
        snapshot = self._client.document(paths.daily_review_path(market_date)).get()
        if not getattr(snapshot, "exists", False):
            return None
        return DailyReview.model_validate(snapshot.to_dict())

    def get_weekly(self, iso_year: int, iso_week: int) -> WeeklyReview | None:
        snapshot = self._client.document(
            paths.weekly_review_path(iso_year, iso_week)
        ).get()
        if not getattr(snapshot, "exists", False):
            return None
        return WeeklyReview.model_validate(snapshot.to_dict())

    def latest_weekly(self) -> WeeklyReview | None:
        """The most recent weekly review, or ``None``.

        Weekly ids sort chronologically as strings (``2026-W03``, decision 26), so taking
        the maximum id is both correct and index-free.
        """
        return self._latest(paths.WEEKLY_REVIEWS, WeeklyReview)

    def latest_daily(self) -> DailyReview | None:
        return self._latest(paths.DAILY_REVIEWS, DailyReview)

    def _latest(self, collection: str, model: type) -> Any | None:
        docs = list(self._client.collection(collection).stream())
        if not docs:
            return None
        return model.model_validate(max(docs, key=lambda doc: doc.id).to_dict())

"""Local high-impact economic-news context for V3 EMA research.

The runtime never reaches out to the web. Operators/research jobs may place a JSON file
containing scheduled releases in the data directory. Each record needs:
{"at":"2026-10-02T12:30:00Z","event":"US NFP","impact":"high"}

Only event timing is used as context/OOD evidence; it never gates a detection.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from aureon.models.base import to_utc

NEWS_PATH_ENV = "AUREON_HIGH_IMPACT_NEWS_PATH"
DEFAULT_NEWS_PATH = "data/high_impact_news.json"


@dataclass(frozen=True)
class NewsContext:
    high_impact: bool = False
    event: str | None = None
    minutes_to_event: float | None = None


class HighImpactNewsTagger:
    def __init__(self, events: tuple[tuple[datetime, str], ...], *, window_minutes: int = 30) -> None:
        self.events = tuple(sorted(events, key=lambda item: item[0]))
        self.window_minutes = max(0, int(window_minutes))

    @classmethod
    def from_file(
        cls,
        path: str | Path | None = None,
        *,
        window_minutes: int = 30,
    ) -> "HighImpactNewsTagger":
        target = Path(path or os.getenv(NEWS_PATH_ENV) or DEFAULT_NEWS_PATH)
        if not target.exists():
            return cls((), window_minutes=window_minutes)
        payload = json.loads(target.read_text(encoding="utf-8"))
        rows = payload.get("events", payload) if isinstance(payload, dict) else payload
        events: list[tuple[datetime, str]] = []
        for row in rows:
            if str(row.get("impact", "high")).lower() != "high":
                continue
            when = datetime.fromisoformat(str(row["at"]).replace("Z", "+00:00"))
            events.append((to_utc(when), str(row.get("event") or "high-impact news")))
        return cls(tuple(events), window_minutes=window_minutes)

    def context_at(self, moment: datetime) -> NewsContext:
        if not self.events:
            return NewsContext()
        now = to_utc(moment)
        nearest: tuple[float, str] | None = None
        for when, event in self.events:
            minutes = (when - now).total_seconds() / 60.0
            distance = abs(minutes)
            if nearest is None or distance < abs(nearest[0]):
                nearest = (minutes, event)
        if nearest is None or abs(nearest[0]) > self.window_minutes:
            return NewsContext()
        return NewsContext(
            high_impact=True,
            event=nearest[1],
            minutes_to_event=nearest[0],
        )

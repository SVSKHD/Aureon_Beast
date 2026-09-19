"""Session summaries (§18).

One document per (broker day, session) at ``sessions/{market_date}__{session}``,
describing a session that has **closed**. A session still in progress is never
written: its high, low and close are all still moving, and a stored partial summary
would be indistinguishable from a final one.

Trend is stored as a label *and* the numeric change that produced it. A label alone
would become unreadable the moment the flat threshold were retuned -- "flat" would
mean one thing for old documents and another for new ones, with no way to tell which.
"""

from __future__ import annotations

from pydantic import Field, model_validator

from aureon.models.base import AureonDocument, MarketTime
from aureon.models.enums import SessionName, Timeframe

TREND_UP = "up"
TREND_DOWN = "down"
TREND_FLAT = "flat"
TRENDS: frozenset[str] = frozenset({TREND_UP, TREND_DOWN, TREND_FLAT})


class SessionSummary(AureonDocument):
    """A completed trading session (§18)."""

    session_id: str = Field(description="{market_date}__{session}")
    account_scope: str
    symbol: str
    timeframe: Timeframe

    session: SessionName
    market_date: str = Field(description="Broker-local date, YYYY-MM-DD.")
    session_config_version: int = Field(
        description="Boundaries are config (decision 7); stamped so a later change "
        "cannot silently reinterpret this document."
    )

    started_at: MarketTime = Field(description="Open of the session's first candle.")
    ended_at: MarketTime = Field(description="Close of the session's last candle.")

    open: float
    high: float
    low: float
    close: float

    trend: str = Field(description=f"One of {sorted(TRENDS)}.")
    change: float = Field(description="close - open, in price.")
    change_points: float = Field(description="close - open, in points.")
    range: float = Field(description="high - low, in price.")
    candle_count: int = Field(ge=1)

    @model_validator(mode="after")
    def _coherent(self) -> SessionSummary:
        if self.trend not in TRENDS:
            raise ValueError(f"trend must be one of {sorted(TRENDS)}, got {self.trend!r}")
        if self.high < self.low:
            raise ValueError(f"session high {self.high} below low {self.low}")
        for name, value in (("open", self.open), ("close", self.close)):
            if not (self.low <= value <= self.high):
                raise ValueError(
                    f"session {name} {value} outside [low {self.low}, high {self.high}]"
                )
        if self.ended_at.utc <= self.started_at.utc:
            raise ValueError("ended_at must be after started_at")
        return self

    @property
    def is_up(self) -> bool:
        return self.trend == TREND_UP

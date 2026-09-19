"""Market data contracts: candles, quotes and symbol metadata (§7-§10).

These are the models every provider must produce, whether it reads a live MT5
terminal or a CSV fixture. Keeping them identical is what makes replay/live
parity testable at all (§82): if the historical provider returned a slightly
different shape, a parity failure could never be distinguished from a data
difference.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from pydantic import Field, computed_field, field_validator, model_validator

from aureon.models.base import AureonModel, MarketTime, UtcDatetime, to_utc, utc_now
from aureon.models.enums import FillingMode, Timeframe


class Candle(AureonModel):
    """One CLOSED candle.

    Providers never return the forming bar. A forming candle's high, low and
    close all still change, so an agent that saw one would emit a detection that
    replay could never reproduce -- the parity test would fail, and worse, a
    detection would encode a price that never finally existed.
    """

    symbol: str
    timeframe: Timeframe
    open_time: MarketTime = Field(description="Candle OPEN, the candle's identity.")
    open: float
    high: float
    low: float
    close: float
    tick_volume: int = 0
    real_volume: int = 0
    spread: int | None = Field(
        default=None, description="Broker-reported spread in points, if available."
    )

    @model_validator(mode="after")
    def _ohlc_must_be_coherent(self) -> Candle:
        if self.high < self.low:
            raise ValueError(f"candle high {self.high} < low {self.low}")
        for name, value in (("open", self.open), ("close", self.close)):
            if not (self.low <= value <= self.high):
                raise ValueError(
                    f"candle {name} {value} outside [low {self.low}, high {self.high}]"
                )
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def close_time(self) -> datetime:
        """The instant this candle stopped accepting ticks (exclusive end)."""
        return self.open_time.utc + timedelta(minutes=self.timeframe.minutes)

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open

    @property
    def upper_wick(self) -> float:
        return self.high - max(self.open, self.close)

    @property
    def lower_wick(self) -> float:
        return min(self.open, self.close) - self.low


class QuoteSnapshot(AureonModel):
    """A bid/ask pair at an instant (§27, §41).

    Carried on a trade request so the confirmation screen and the execution-time
    guard can both reason about the SAME quote and about how old it has become.
    A quote's age is the difference between a confirmation a human understood and
    one they did not.
    """

    symbol: str
    bid: float
    ask: float
    captured_at: UtcDatetime = Field(default_factory=utc_now)
    point: float | None = Field(
        default=None, description="Symbol point size, so spread can be given in points."
    )

    @field_validator("captured_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return to_utc(value)

    @model_validator(mode="after")
    def _ask_not_below_bid(self) -> QuoteSnapshot:
        if self.ask < self.bid:
            raise ValueError(f"ask {self.ask} below bid {self.bid} for {self.symbol}")
        return self

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread_points(self) -> float | None:
        """Spread in points, or ``None`` when point size is unknown.

        Returns ``None`` rather than guessing a point size: the spread guard
        (§41) must fail closed on an unknown symbol, not compare against a
        fabricated scale.
        """
        if not self.point:
            return None
        return self.spread / self.point

    def age_seconds(self, *, now: datetime | None = None) -> float:
        return (to_utc(now or utc_now()) - self.captured_at).total_seconds()

    def is_stale(self, ttl_seconds: float, *, now: datetime | None = None) -> bool:
        return self.age_seconds(now=now) > ttl_seconds

    def price_for(self, *, is_buy: bool) -> float:
        """The price this side of the book would actually transact at."""
        return self.ask if is_buy else self.bid


class SymbolInfo(AureonModel):
    """Broker metadata for a symbol (§39, §42).

    Everything needed to validate an order *before* sending it: what volumes are
    legal, how close a stop may sit, and which filling modes the broker will
    accept. Phase 6 builds the execution-mode selector directly from
    ``filling_modes`` so a human is never offered FOK on a symbol that rejects it.
    """

    symbol: str
    point: float = Field(gt=0, description="Smallest price increment.")
    digits: int = Field(ge=0)
    volume_min: float = Field(gt=0)
    volume_max: float = Field(gt=0)
    volume_step: float = Field(gt=0)
    stops_level: int = Field(
        default=0,
        ge=0,
        description="Minimum stop distance from price, in POINTS (§42).",
    )
    filling_modes: tuple[FillingMode, ...] = ()
    trade_mode: str = Field(
        default="unknown",
        description="Broker trade mode: full / close_only / disabled (§10).",
    )
    spread: int | None = Field(default=None, description="Current spread in points.")

    @model_validator(mode="after")
    def _volume_bounds_coherent(self) -> SymbolInfo:
        if self.volume_max < self.volume_min:
            raise ValueError(
                f"{self.symbol}: volume_max {self.volume_max} < volume_min {self.volume_min}"
            )
        return self

    @property
    def is_tradeable(self) -> bool:
        """Whether the broker will accept a NEW position on this symbol.

        ``close_only`` is deliberately not tradeable: it permits closing an
        existing position but not opening one, and treating it as open would
        surface as an opaque broker rejection after a human had confirmed.
        """
        return self.trade_mode == "full"

    def supports(self, mode: FillingMode) -> bool:
        return mode in self.filling_modes

    @property
    def min_stop_distance(self) -> float:
        """Minimum stop distance expressed in PRICE, not points (§42)."""
        return self.stops_level * self.point

    def normalize_volume(self, volume: float) -> float:
        """Validate a volume against the broker's grid, rejecting bad input.

        Deliberately raises instead of rounding. CLAUDE.md and §42 require that a
        volume off the broker's step be rejected: silently rounding 0.15 to 0.10
        would execute a trade the human did not ask for, and rounding up would
        risk more of their money than they authorised.
        """
        if volume < self.volume_min:
            raise ValueError(
                f"{self.symbol}: volume {volume} below minimum {self.volume_min}"
            )
        if volume > self.volume_max:
            raise ValueError(
                f"{self.symbol}: volume {volume} above maximum {self.volume_max}"
            )
        steps = (volume - self.volume_min) / self.volume_step
        # Tolerance absorbs binary float error (0.1 + 0.2 != 0.3), not real
        # off-grid input: 0.15 on a 0.10 step is still rejected.
        if abs(steps - round(steps)) > 1e-6:
            raise ValueError(
                f"{self.symbol}: volume {volume} is not a multiple of "
                f"volume_step {self.volume_step} above volume_min {self.volume_min}"
            )
        return round(self.volume_min + round(steps) * self.volume_step, 8)

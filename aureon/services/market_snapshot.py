"""Accumulating what ``system_state`` reports about a symbol (§59, §66).

``/status`` and, later, a dashboard both need the same picture: where the EMAs are, what
RSI is doing, which session it is, how many crosses there have been today, and when each
kind of event last happened. This assembles that from the detections the engine already
produces, so the observer does not recompute an indicator it has already computed and
the two cannot disagree.

## Why it reads detections rather than the window

An indicator computed a second time here would be a second implementation, and the first
time the two differed the status screen would quietly be wrong about a number a human
was using to decide something. Every value below comes from a detection's own
``IndicatorSnapshot`` or from the engine's counters -- the same values that were stored.

The consequence is honest and worth stating: a field stays ``None`` until an agent has
emitted a detection carrying it. On a fresh start the screen says "unknown" rather than
guessing, which is the correct answer to "what is RSI doing?" before anything has looked.

## Not a tick stream

Everything here is current state, overwritten in place, written on candle close. It is
the same distinction ``SymbolState.last_quote`` rests on (decision 80): one snapshot per
symbol, never an append.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from aureon.agents.rsi_agent import ZONE_NEUTRAL, ZONE_OVERBOUGHT, ZONE_OVERSOLD
from aureon.models.detection import Detection
from aureon.models.enums import Direction, SessionName

#: Agent names this reads. Named so a rename breaks here rather than silently producing a
#: screen that stopped updating one panel.
AGENT_CROSS = "ema_cross"
AGENT_RSI = "rsi"
AGENT_LIQUIDITY = "liquidity"
AGENT_WICK = "wick"
AGENT_BREAKOUT = "breakout"
AGENT_SESSION_TREND = "session_trend"

RSI_ZONES = frozenset({ZONE_OVERBOUGHT, ZONE_OVERSOLD, ZONE_NEUTRAL})


@dataclass
class MarketSnapshot:
    """Current state for one symbol/timeframe, as ``SymbolState`` names it.

    Mutable and long-lived: the observer keeps one per symbol and feeds detections into
    it. Reset on a market-date change, like the engine's counters, so "today" means the
    broker's today.
    """

    symbol: str
    market_date: str | None = None

    ema_fast: float | None = None
    ema_slow: float | None = None
    rsi: float | None = None

    session: SessionName | None = None
    session_trend: str | None = None
    session_high: float | None = None
    session_low: float | None = None

    last_cross: dict[str, object] | None = None
    last_cross_at: datetime | None = None
    last_sweep: dict[str, object] | None = None
    last_wick: dict[str, object] | None = None
    last_breakout: dict[str, object] | None = None

    detections_today: int = 0
    _seen: set[str] = field(default_factory=set, repr=False)

    # ── Derived ───────────────────────────────────────────────────────────────

    @property
    def ema_distance(self) -> float | None:
        """``fast - slow``, in price. Sign is the current bias.

        ``None`` unless BOTH are known: a distance computed from one EMA and a missing
        one would read as a number, and a reader has no way to tell it apart from a real
        one.
        """
        if self.ema_fast is None or self.ema_slow is None:
            return None
        return round(self.ema_fast - self.ema_slow, 8)

    @property
    def rsi_zone(self) -> str | None:
        """Derived here rather than stored, so nothing can carry a stale zone.

        Uses the shared ``rsi_zone`` helper so /status and the RSI agent cannot disagree
        about where overbought begins.
        """
        if self.rsi is None:
            return None
        from aureon.agents.rsi_agent import rsi_zone

        return rsi_zone(self.rsi, overbought=70.0, oversold=30.0)

    # ── Feeding ───────────────────────────────────────────────────────────────

    def observe(self, detection: Detection) -> None:
        """Fold one detection into the snapshot.

        Idempotent per detection id: the observer's restart backfill can replay a candle
        it already processed, and counting it twice would make ``detections_today`` drift
        upward with nothing to reveal it.
        """
        if detection.detection_id in self._seen:
            return

        market_date = detection.detected_at.market_date
        if self.market_date != market_date:
            self.roll_day(market_date)

        self._seen.add(detection.detection_id)
        self.detections_today += 1
        self.session = detection.session.session

        # Indicators come from the detection's own snapshot -- the stored values, not a
        # recomputation.
        ema = detection.indicators.ema
        if "fast" in ema:
            self.ema_fast = ema["fast"]
        if "slow" in ema:
            self.ema_slow = ema["slow"]
        if detection.indicators.rsi is not None:
            self.rsi = detection.indicators.rsi

        at = detection.detected_at.utc
        if detection.agent_name == AGENT_CROSS and detection.direction is not None:
            self.last_cross = {
                "direction": detection.direction.value,
                "at": at.isoformat(),
                "price": detection.price,
                "detection_id": detection.detection_id,
            }
            self.last_cross_at = at
        elif detection.agent_name == AGENT_LIQUIDITY:
            direction, _, level_type = detection.event_key.partition("|")
            self.last_sweep = {
                "direction": direction,
                "level_type": level_type,
                "at": at.isoformat(),
            }
        elif detection.agent_name == AGENT_BREAKOUT:
            direction, _, level_type = detection.event_key.partition("|")
            self.last_breakout = {
                "direction": direction,
                "level_type": level_type,
                "at": at.isoformat(),
            }
        elif detection.agent_name == AGENT_WICK:
            self.last_wick = {"classification": detection.event_key, "at": at.isoformat()}
        elif detection.agent_name == AGENT_SESSION_TREND:
            _, _, trend = detection.event_key.partition("|")
            self.session_trend = trend

    def observe_candle(self, *, high: float, low: float, session: SessionName) -> None:
        """Track the session's running high and low.

        From candles rather than from detections, because a session can have a high
        without anything detecting anything -- and a session_high that only moved when an
        agent fired would be wrong most of the time.
        """
        if session is not self.session and self.session is not None:
            self.session_high = None
            self.session_low = None
        self.session = session
        self.session_high = high if self.session_high is None else max(self.session_high, high)
        self.session_low = low if self.session_low is None else min(self.session_low, low)

    def roll_day(self, market_date: str) -> None:
        """A new broker day. Clears what "today" means and the session extremes.

        Indicator values are deliberately KEPT: an EMA does not reset at midnight, and
        blanking it would make the screen claim no information on the first candle of
        every day.
        """
        self.market_date = market_date
        self.detections_today = 0
        self._seen.clear()
        self.session_high = None
        self.session_low = None
        self.session_trend = None

    # ── Output ────────────────────────────────────────────────────────────────

    def as_state(self) -> dict[str, object]:
        """The snapshot as ``SymbolState`` field names."""
        return {
            "ema_fast": self.ema_fast,
            "ema_slow": self.ema_slow,
            "ema_distance": self.ema_distance,
            "rsi": self.rsi,
            "rsi_zone": self.rsi_zone,
            "session": self.session,
            "session_trend": self.session_trend,
            "session_high": self.session_high,
            "session_low": self.session_low,
            "last_cross": self.last_cross,
            "last_cross_at": self.last_cross_at,
            "last_sweep": self.last_sweep,
            "last_wick": self.last_wick,
            "last_breakout": self.last_breakout,
            "detections_today": self.detections_today,
        }


def direction_label(direction: Direction | str | None) -> str:
    """A direction rendered for a human, or "—" when there is nothing to say."""
    if direction is None:
        return "—"
    return direction.value if isinstance(direction, Direction) else str(direction)

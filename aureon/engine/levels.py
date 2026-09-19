"""The single source of price levels (§15, §17).

**One implementation, shared.** The liquidity agent (which watches levels being swept)
and the breakout agent (which watches them being broken) must agree on where a level
*is*, or the two will disagree about the same bar -- one reporting a sweep of a high
the other never considered a high. The spec is explicit that there must never be two
implementations, so both agents call this module and neither computes a level itself.

Levels are computed **purely from the window**, with no accumulated state, so an agent
using them stays a pure function of ``(window, ctx)`` and replay/live parity holds.

## What counts as a swing

A swing high is a *fractal pivot*: a bar whose high is strictly greater than the highs
of ``strength`` bars on each side. Requiring strictly greater on both sides means a
flat-topped double bar is not a pivot, which is deliberate -- an equal high is a level
that has already been tested, and treating it as a fresh pivot double-counts it.

Crucially, a pivot can only be confirmed once ``strength`` bars have closed *after* it.
The most recent ``strength`` bars therefore never contain a confirmed pivot, and this
module does not report one there. Reporting an unconfirmed pivot would be using future
information: the bar might yet be exceeded, and the "level" would vanish.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from aureon.config.sessions import sessions_for_index
from aureon.models.enums import SessionName

DEFAULT_SWING_STRENGTH = 3

# Level type labels. These appear in agents' event_keys, so they are part of the
# stored contract -- renaming one re-keys detections.
LEVEL_SWING_HIGH = "swing_high"
LEVEL_SWING_LOW = "swing_low"
LEVEL_PREVIOUS_DAY_HIGH = "previous_day_high"
LEVEL_PREVIOUS_DAY_LOW = "previous_day_low"
LEVEL_PREVIOUS_SESSION_HIGH = "previous_session_high"
LEVEL_PREVIOUS_SESSION_LOW = "previous_session_low"
LEVEL_ASIA_HIGH = "asia_high"
LEVEL_ASIA_LOW = "asia_low"
LEVEL_LONDON_HIGH = "london_high"
LEVEL_LONDON_LOW = "london_low"

HIGH_LEVELS: frozenset[str] = frozenset(
    {
        LEVEL_SWING_HIGH,
        LEVEL_PREVIOUS_DAY_HIGH,
        LEVEL_PREVIOUS_SESSION_HIGH,
        LEVEL_ASIA_HIGH,
        LEVEL_LONDON_HIGH,
    }
)
LOW_LEVELS: frozenset[str] = frozenset(
    {
        LEVEL_SWING_LOW,
        LEVEL_PREVIOUS_DAY_LOW,
        LEVEL_PREVIOUS_SESSION_LOW,
        LEVEL_ASIA_LOW,
        LEVEL_LONDON_LOW,
    }
)


@dataclass(frozen=True)
class Level:
    """One price level and where it came from."""

    level_type: str
    price: float
    formed_at: datetime | None = None

    @property
    def is_high(self) -> bool:
        return self.level_type in HIGH_LEVELS


@dataclass(frozen=True)
class Levels:
    """Every level known at a given candle."""

    levels: dict[str, Level] = field(default_factory=dict)

    def get(self, level_type: str) -> Level | None:
        return self.levels.get(level_type)

    def price(self, level_type: str) -> float | None:
        level = self.levels.get(level_type)
        return level.price if level else None

    @property
    def highs(self) -> dict[str, Level]:
        return {k: v for k, v in self.levels.items() if v.is_high}

    @property
    def lows(self) -> dict[str, Level]:
        return {k: v for k, v in self.levels.items() if not v.is_high}

    def as_prices(self) -> dict[str, float]:
        """Flat name→price map, for storing on a detection."""
        return {k: v.price for k, v in self.levels.items()}

    def __len__(self) -> int:
        return len(self.levels)


class LevelTracker:
    """Derives price levels from a candle window (§15, §17)."""

    def __init__(self, *, swing_strength: int = DEFAULT_SWING_STRENGTH) -> None:
        if swing_strength < 1:
            raise ValueError("swing_strength must be at least 1")
        self.swing_strength = swing_strength

    def params_snapshot(self) -> dict[str, object]:
        return {"swing_strength": self.swing_strength, "level_tracker_version": "1.0.0"}

    def min_window(self, *, bars_per_day: int) -> int:
        """Bars needed to know the previous day's range.

        Two days plus a pivot's confirmation margin: the previous day must be fully
        present, and the current day may already be underway.
        """
        return bars_per_day * 2 + self.swing_strength * 2 + 1

    # ── Entry point ───────────────────────────────────────────────────────────

    def levels_for(self, window: pd.DataFrame, market_tz: str) -> Levels:
        """Every level known as of the window's LAST bar.

        Uses only bars at or before that point -- and for pivots, only bars old enough
        to be confirmed -- so nothing here depends on future information.
        """
        if len(window) < 2:
            return Levels()

        # Converted in one vectorised pandas call rather than by building a MarketTime
        # per bar. Constructing ~580 validated models per candle made this the single
        # largest cost in the Part B suite; the arithmetic is identical.
        market_index = window.index.tz_convert(market_tz)
        # Day keys are OPAQUE INTEGERS, not formatted dates: they are only ever
        # compared for equality here (grouping bars into broker days), never shown.
        # Measured against the alternatives, per-element strftime cost 1.6s and
        # DatetimeIndex.strftime 4.9s over the same workload, against 0.25s for this.
        # tz_localize(None) first so a DST transition day cannot make floor("D")
        # ambiguous -- the local calendar date is what a broker day means.
        dates = market_index.tz_localize(None).floor("D").astype("int64").tolist()
        sessions = sessions_for_index(market_index)

        found: dict[str, Level] = {}
        found.update(self._swings(window))
        found.update(self._previous_day(window, dates))
        found.update(self._previous_session(window, sessions))
        found.update(self._named_sessions(window, dates, sessions))
        return Levels(found)

    # ── Swings ────────────────────────────────────────────────────────────────

    def _swings(self, window: pd.DataFrame) -> dict[str, Level]:
        """The most recent CONFIRMED swing high and low."""
        strength = self.swing_strength
        highs = window["high"].to_numpy()
        lows = window["low"].to_numpy()
        stamps = window.index

        out: dict[str, Level] = {}
        # Scan backwards from the newest bar that could be confirmed: a pivot needs
        # `strength` bars after it, so the last `strength` bars are never eligible.
        last_eligible = len(window) - 1 - strength
        for i in range(last_eligible, strength - 1, -1):
            if LEVEL_SWING_HIGH not in out and self._is_pivot_high(highs, i, strength):
                out[LEVEL_SWING_HIGH] = Level(
                    LEVEL_SWING_HIGH, float(highs[i]), stamps[i].to_pydatetime()
                )
            if LEVEL_SWING_LOW not in out and self._is_pivot_low(lows, i, strength):
                out[LEVEL_SWING_LOW] = Level(
                    LEVEL_SWING_LOW, float(lows[i]), stamps[i].to_pydatetime()
                )
            if len(out) == 2:
                break
        return out

    @staticmethod
    def _is_pivot_high(values: pd.Series | object, index: int, strength: int) -> bool:
        centre = values[index]
        for offset in range(1, strength + 1):
            # Strictly greater on BOTH sides: an equal high is an already-tested level,
            # not a fresh pivot.
            if not (centre > values[index - offset] and centre > values[index + offset]):
                return False
        return True

    @staticmethod
    def _is_pivot_low(values: pd.Series | object, index: int, strength: int) -> bool:
        centre = values[index]
        for offset in range(1, strength + 1):
            if not (centre < values[index - offset] and centre < values[index + offset]):
                return False
        return True

    # ── Day and session ranges ────────────────────────────────────────────────

    @staticmethod
    def _previous_day(window: pd.DataFrame, dates: list[int]) -> dict[str, Level]:
        """High and low of the most recent COMPLETED broker day.

        "Completed" means a day before the current bar's day -- so the level is stable
        and cannot change as the current day progresses.
        """
        current = dates[-1]
        indices = [i for i, d in enumerate(dates) if d != current]
        if not indices:
            return {}
        previous_date = dates[indices[-1]]
        rows = [i for i, d in enumerate(dates) if d == previous_date]
        # Only count it if the day is fully inside the window; a truncated day would
        # report a high that merely happens to be the window's edge.
        if rows[0] == 0:
            return {}
        block = window.iloc[rows[0] : rows[-1] + 1]
        formed = window.index[rows[-1]].to_pydatetime()
        return {
            LEVEL_PREVIOUS_DAY_HIGH: Level(
                LEVEL_PREVIOUS_DAY_HIGH, float(block["high"].max()), formed
            ),
            LEVEL_PREVIOUS_DAY_LOW: Level(
                LEVEL_PREVIOUS_DAY_LOW, float(block["low"].min()), formed
            ),
        }

    @staticmethod
    def _previous_session(
        window: pd.DataFrame, sessions: list[SessionName]
    ) -> dict[str, Level]:
        """High and low of the session immediately before the current one."""
        current = sessions[-1]
        end = len(sessions) - 1
        while end >= 0 and sessions[end] == current:
            end -= 1
        if end < 0:
            return {}
        target = sessions[end]
        start = end
        while start >= 0 and sessions[start] == target:
            start -= 1
        if start < 0:
            return {}  # truncated: the session began before the window
        block = window.iloc[start + 1 : end + 1]
        formed = window.index[end].to_pydatetime()
        return {
            LEVEL_PREVIOUS_SESSION_HIGH: Level(
                LEVEL_PREVIOUS_SESSION_HIGH, float(block["high"].max()), formed
            ),
            LEVEL_PREVIOUS_SESSION_LOW: Level(
                LEVEL_PREVIOUS_SESSION_LOW, float(block["low"].min()), formed
            ),
        }

    @staticmethod
    def _named_sessions(
        window: pd.DataFrame, dates: list[int], sessions: list[SessionName]
    ) -> dict[str, Level]:
        """Asia and London ranges for the CURRENT broker day, once complete.

        Only reported after the session has ended, for the same reason the session
        summary is only written at the close: a range that is still forming is not yet
        a level, and a breakout agent acting on one would be reacting to noise.
        """
        current_date = dates[-1]
        current_session = sessions[-1]
        wanted = {
            SessionName.ASIA: (LEVEL_ASIA_HIGH, LEVEL_ASIA_LOW),
            SessionName.LONDON: (LEVEL_LONDON_HIGH, LEVEL_LONDON_LOW),
        }

        out: dict[str, Level] = {}
        for session, (high_name, low_name) in wanted.items():
            if session == current_session:
                continue  # still in progress today
            rows = [
                i
                for i, (d, s) in enumerate(zip(dates, sessions, strict=False))
                if d == current_date and s == session
            ]
            if not rows or rows[0] == 0:
                continue
            block = window.iloc[rows[0] : rows[-1] + 1]
            formed = window.index[rows[-1]].to_pydatetime()
            out[high_name] = Level(high_name, float(block["high"].max()), formed)
            out[low_name] = Level(low_name, float(block["low"].min()), formed)
        return out

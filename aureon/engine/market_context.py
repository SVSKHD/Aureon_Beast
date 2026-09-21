"""Per-symbol volume-profile and volatility context, maintained candle by candle (9B, §19).

The engine owns one tracker per ``(symbol, timeframe)``. On every closed candle it updates its
buffers and can then answer two questions, both **only from candles that have already
closed**:

* what the session profiles look like (``profiles()``), for ``SystemState`` and ``/status``;
* what a detection at this candle should record (``reference()``, ``volatility()``).

## Why the reference scope is ASIA

A detection carries one ``volume_profile_ref``, so its scope is a choice. It is Asia's, for
three reasons that all point the same way:

* the overnight range is what the rest of the day trades against, which is why the §19 tags
  are written in terms of it (`price_above_asia_va`);
* Asia's profile is **retained until the broker day ends**, so the field means the same thing
  at 09:00 and at 22:00;
* a *developing day* profile would not. At 09:00 "today's value area" IS Asia's; by 17:00 it
  is everything. One field name meaning two things at two hours is the worst property a
  research field can have -- every grouping across the day would silently mix them.

Before Asia has traded on a broker day the reference is ``None``, not an empty profile: absent
context is a gap a reader can see, where an empty one looks like a measurement that found
nothing.

## Computed once per candle, not once per detection

``observe`` invalidates a small cache; ``reference`` and ``volatility`` fill it. Without that,
a candle producing four detections rebuilt the profile four times and re-ran the ATR recursion
four times -- which took the agent suite from seconds to nearly three minutes and would have
done the same to every replay. The profile does not depend on which detection is asking; only
the last cheap step (where this price sits relative to the value area) does.

## Every figure a DETECTION carries comes from the broker day's candles

Deliberately, and it is a parity property rather than a preference. ``session_verify`` compares
a live session's stored detections against a replay of that day's archive (§82); if the context
depended on a rolling 24h buffer, the live run -- whose reach-back may have loaded candles from
before the archive begins -- would hold more history than the replay and every context field in
the first candles would differ. Scoping to the broker day makes the context a pure function of
the candles the archive holds.

The cost is honest and small: ATR is absent for the first fourteen candles of a broker day
rather than borrowing yesterday's. An absent number for seventy minutes is better than one
that cannot be reproduced. ``rolling_24h`` still exists as a profile scope for the live panel,
where it is labelled and never compared.

## Buffers, and why they are bounded

Each tracker holds the current broker day's candles, the current day's candles per session,
and a rolling 24h deque. All bounded: an observer runs for weeks, and an unbounded buffer is
how a process that was fine on Friday is killed on Tuesday. The 20-day session-range history
holds ranges (floats), not candles, for the same reason.
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from collections.abc import Sequence

from aureon.config.sessions import session_for
from aureon.config.symbol_tuning import SymbolTuning
from aureon.engine.volatility import MEDIAN_SESSION_DAYS, build_context
from aureon.engine.volume_profile import build_profile, reference_for
from aureon.models.enums import SessionName, Timeframe
from aureon.models.market import Candle
from aureon.models.profile import VolatilityContext, VolumeProfile, VolumeProfileRef

log = logging.getLogger(__name__)

#: One broker day of M5 candles is 288; the cap is generous for a slower timeframe and still
#: bounded. A day that somehow ran longer would drop its oldest candles rather than the
#: process running out of memory.
MAX_DAY_CANDLES = 2_000
#: The rolling 24h window, in candles. Same reasoning.
MAX_ROLLING_CANDLES = 2_000

#: The scope a detection's reference is taken from. See the module docstring.
DETECTION_SCOPE = "asia"


class MarketContextTracker:
    """Volume profile and volatility for one symbol and timeframe (9B)."""

    def __init__(
        self,
        *,
        symbol: str,
        timeframe: Timeframe,
        tuning: SymbolTuning,
    ) -> None:
        self.symbol = symbol
        self.timeframe = timeframe
        self.tuning = tuning

        self._market_date: str | None = None
        self._session: SessionName | None = None
        self._day: deque[Candle] = deque(maxlen=MAX_DAY_CANDLES)
        self._rolling: deque[Candle] = deque(maxlen=MAX_ROLLING_CANDLES)
        self._by_session: dict[SessionName, list[Candle]] = defaultdict(list)
        self._previous_session: SessionName | None = None
        #: session -> that session's range on each of the previous broker days.
        self._session_history: dict[SessionName, deque[float]] = defaultdict(
            lambda: deque(maxlen=MEDIAN_SESSION_DAYS)
        )
        #: Cleared by ``observe``: the Asia profile and the volatility context for the
        #: candle just closed, so N detections from one candle cost one computation.
        self._cached_asia: VolumeProfile | None = None
        self._cached_volatility: VolatilityContext | None = None

    # ── Updating ──────────────────────────────────────────────────────────────

    def observe(self, candle: Candle) -> None:
        """Fold one CLOSED candle in.

        Called by the engine before it builds any context for that candle, so the candle
        that produced a detection is part of the profile the detection records -- it has
        closed, so it is information the moment describes.
        """
        market_date = candle.open_time.market_date
        session = session_for(candle.open_time.market)

        if self._market_date != market_date:
            self._roll_day()
            self._market_date = market_date
        if self._session != session:
            # The session that just ended becomes `previous_session`; its candles stay in
            # `_by_session` until the day rolls, which is what "Asia retained until day
            # end" means.
            if self._session is not None:
                self._previous_session = self._session
            self._session = session

        self._day.append(candle)
        self._rolling.append(candle)
        self._by_session[session].append(candle)
        self._cached_asia = None
        self._cached_volatility = None

    def _roll_day(self) -> None:
        """Close the broker day: record each session's range, then clear the day."""
        for session, candles in self._by_session.items():
            if not candles:
                continue
            span = max(c.high for c in candles) - min(c.low for c in candles)
            if span > 0:
                self._session_history[session].append(span)
        self._by_session.clear()
        self._day.clear()
        self._previous_session = None

    # ── Reading ───────────────────────────────────────────────────────────────

    def profile(self, scope: str) -> VolumeProfile:
        """The profile for one scope, from candles that have already closed."""
        return build_profile(
            self._candles_for(scope),
            bin_points=self.tuning.volume_bin_points,
            point=self.tuning.point,
            scope=scope,
            symbol=self.symbol,
            timeframe=self.timeframe,
        )

    def _candles_for(self, scope: str) -> Sequence[Candle]:
        if scope == "day":
            return list(self._day)
        if scope == "rolling_24h":
            return list(self._rolling)
        if scope == "previous_session":
            if self._previous_session is None:
                return []
            return list(self._by_session.get(self._previous_session, []))
        for session in SessionName:
            if session.value == scope:
                return list(self._by_session.get(session, []))
        raise ValueError(f"unknown profile scope {scope!r}")

    def profiles(self) -> dict[str, VolumeProfile]:
        """What ``SystemState`` carries: the session in progress, Asia, and the day (9B)."""
        current = self._session.value if self._session else DETECTION_SCOPE
        if current == SessionName.OFF.value:
            # OFF is not a profile scope: it is the absence of a session, and a profile
            # labelled `off` would invite reading it as one.
            current = DETECTION_SCOPE
        return {
            "current_session": self.profile(current),
            "asia": self.profile(DETECTION_SCOPE),
            "day": self.profile("day"),
        }

    def recent(self, count: int) -> list[Candle]:
        """The last ``count`` closed candles, oldest first (9D).

        From the ROLLING window rather than the day's, because the trend read is about what
        the market has been doing and a read taken twenty minutes after the day boundary
        should not be looking at four candles. The day-scoped windows exist for the volume
        profile, where the boundary is the point (decision 172).
        """
        if count <= 0:
            return []
        return list(self._rolling)[-count:]

    def reference(self, price: float) -> VolumeProfileRef | None:
        """What a detection at ``price`` records, or ``None`` before Asia has traded.

        The profile is cached for this candle; only the price comparison is per detection.
        """
        if self._cached_asia is None:
            self._cached_asia = self.profile(DETECTION_SCOPE)
        if self._cached_asia.is_empty:
            return None
        return reference_for(self._cached_asia, price)

    def volatility(self) -> VolatilityContext:
        """ATR and the session range against this session's own 20-day median (9B).

        ATR comes from the **broker day's** candles, not the rolling 24h buffer, so a replay
        of one day's archive reproduces it exactly -- see the module docstring.
        """
        if self._cached_volatility is not None:
            return self._cached_volatility
        session = self._session
        self._cached_volatility = build_context(
            list(self._day),
            point=self.tuning.point,
            tuning=self.tuning,
            session_candles=list(self._by_session.get(session, [])) if session else None,
            previous_session_ranges=(
                list(self._session_history.get(session, ())) if session else ()
            ),
        )
        return self._cached_volatility

    # ── Diagnostics ───────────────────────────────────────────────────────────

    @property
    def market_date(self) -> str | None:
        return self._market_date

    @property
    def session(self) -> SessionName | None:
        return self._session

    def days_of_history(self, session: SessionName) -> int:
        """How many previous days' ranges the median for ``session`` rests on."""
        return len(self._session_history.get(session, ()))

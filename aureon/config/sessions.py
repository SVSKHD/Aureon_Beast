"""Session boundaries (§18, decision 7).

Placeholder defaults, stated plainly as such: Asia 02-10, London 10-18, New York
15-23 in *market* time. They are almost certainly not exactly the boundaries a
trader would draw, which is why ``SESSION_CONFIG_VERSION`` exists -- every
detection stamps it, so a later correction re-tags new detections without
silently reinterpreting old ones.

Sessions overlap (London 10-18 and New York 15-23 share 15-18), so a candle can
fall in two. Decision 7 resolves that with a fixed precedence -- London, then New
York, then Asia -- rather than by ordering the dict, because relying on
declaration order would make session assignment sensitive to an unrelated edit.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

from aureon.models.enums import SessionName

# Bump on ANY change to the windows or the precedence below (decision 7).
SESSION_CONFIG_VERSION = 1


class SessionWindow:
    """A session's half-open window ``[start, end)`` in market-local time."""

    __slots__ = ("name", "start", "end")

    def __init__(self, name: SessionName, start: time, end: time) -> None:
        self.name = name
        self.start = start
        self.end = end

    def contains(self, moment: time) -> bool:
        # Half-open so a boundary minute belongs to exactly one window: with
        # Asia ending at 10:00 and London starting at 10:00, 10:00 is London's.
        if self.start <= self.end:
            return self.start <= moment < self.end
        # Wrap past midnight (not used by the defaults, but a real possibility
        # once these are tuned to a broker whose day starts in the evening).
        return moment >= self.start or moment < self.end

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"SessionWindow({self.name}, {self.start}-{self.end})"


SESSION_WINDOWS: dict[SessionName, SessionWindow] = {
    SessionName.ASIA: SessionWindow(SessionName.ASIA, time(2, 0), time(10, 0)),
    SessionName.LONDON: SessionWindow(SessionName.LONDON, time(10, 0), time(18, 0)),
    SessionName.NEW_YORK: SessionWindow(SessionName.NEW_YORK, time(15, 0), time(23, 0)),
}

# Decision 7: explicit precedence for overlapping windows.
SESSION_PRECEDENCE: tuple[SessionName, ...] = (
    SessionName.LONDON,
    SessionName.NEW_YORK,
    SessionName.ASIA,
)


def session_for(market_dt: datetime) -> SessionName:
    """The session a MARKET-LOCAL datetime falls in (§18).

    The argument must already be in market time -- pass ``MarketTime.market``, not
    ``.utc``. Passing UTC would silently shift every boundary by the broker's
    offset, which is the kind of bug that only shows up as "London detections
    look odd on Mondays".
    """
    moment = market_dt.time()
    for name in SESSION_PRECEDENCE:
        if SESSION_WINDOWS[name].contains(moment):
            return name
    return SessionName.OFF


def session_close(market_dt: datetime) -> datetime | None:
    """When the session containing ``market_dt`` closes, in market time.

    Used by the ``session_close`` evaluation horizon (§21). Returns ``None``
    outside any session, where there is no close to wait for.
    """
    name = session_for(market_dt)
    if name is SessionName.OFF:
        return None
    window = SESSION_WINDOWS[name]
    close = market_dt.replace(
        hour=window.end.hour, minute=window.end.minute, second=0, microsecond=0
    )
    if close <= market_dt:
        close += timedelta(days=1)
    return close


def day_close(market_dt: datetime) -> datetime:
    """Broker-day close: midnight at the end of this market day (§21)."""
    return market_dt.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)


def session_for_minute_of_day(minute_of_day: int) -> SessionName:
    """Session for a market-local minute-of-day (0-1439).

    Same windows and same precedence as ``session_for`` -- it reads
    ``SESSION_WINDOWS`` and ``SESSION_PRECEDENCE``, so there is still one definition
    of where the boundaries are. This variant exists only so a caller with thousands
    of timestamps can avoid materialising a ``datetime`` per bar, which profiling
    showed dominated the level and session agents.

    ``test_part_b_agents`` asserts the two agree for every minute of the day.
    """
    for name in SESSION_PRECEDENCE:
        window = SESSION_WINDOWS[name]
        start = window.start.hour * 60 + window.start.minute
        end = window.end.hour * 60 + window.end.minute
        if start <= end:
            if start <= minute_of_day < end:
                return name
        elif minute_of_day >= start or minute_of_day < end:
            return name
    return SessionName.OFF


def sessions_for_index(market_index: object) -> list[SessionName]:
    """Sessions for a tz-aware pandas DatetimeIndex already in MARKET time.

    The index must already be converted; passing a UTC index would shift every
    boundary by the broker's offset (§18).
    """
    hours = market_index.hour  # type: ignore[attr-defined]
    minutes = market_index.minute  # type: ignore[attr-defined]
    # Cached per distinct minute-of-day: a week of M5 bars has at most 288 distinct
    # values, so the window lookup runs a few hundred times instead of per bar.
    cache: dict[int, SessionName] = {}
    out: list[SessionName] = []
    for hour, minute in zip(hours, minutes, strict=True):
        key = int(hour) * 60 + int(minute)
        session = cache.get(key)
        if session is None:
            session = session_for_minute_of_day(key)
            cache[key] = session
        out.append(session)
    return out

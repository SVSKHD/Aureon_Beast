"""Period boundaries on the market clock (§61, §63).

A review covers a **broker** trading day or week, not a UTC one. 23:30 UTC is already the
next day in Athens, so a UTC-bounded review would file a session under the wrong day and
every per-day figure would be subtly wrong in a way nothing in the output reveals.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from aureon.models.base import to_utc


@dataclass(frozen=True)
class Period:
    """A half-open ``[start, end)`` window in UTC, labelled by its market-local identity."""

    start: datetime
    end: datetime
    market_date: str
    iso_year: int
    iso_week: int

    def contains(self, moment: datetime) -> bool:
        return self.start <= to_utc(moment) < self.end


def day_period(market_date: str | date, market_tz: str) -> Period:
    """The UTC window covering one broker trading day.

    Half-open so consecutive days tile without double-counting the midnight boundary.
    """
    day = date.fromisoformat(market_date) if isinstance(market_date, str) else market_date
    zone = ZoneInfo(market_tz)
    start_local = datetime.combine(day, time(0, 0), tzinfo=zone)
    end_local = start_local + timedelta(days=1)
    iso = day.isocalendar()
    return Period(
        start=to_utc(start_local),
        end=to_utc(end_local),
        market_date=day.isoformat(),
        iso_year=iso.year,
        iso_week=iso.week,
    )


def week_period(iso_year: int, iso_week: int, market_tz: str) -> Period:
    """The UTC window covering one ISO trading week, Monday to Monday.

    ISO weeks rather than a broker-specific convention, because ISO is unambiguous across
    year boundaries -- where "week 1" otherwise means different things to different readers.
    """
    zone = ZoneInfo(market_tz)
    monday = date.fromisocalendar(iso_year, iso_week, 1)
    start_local = datetime.combine(monday, time(0, 0), tzinfo=zone)
    end_local = start_local + timedelta(days=7)
    return Period(
        start=to_utc(start_local),
        end=to_utc(end_local),
        market_date=monday.isoformat(),
        iso_year=iso_year,
        iso_week=iso_week,
    )


def market_date_of(moment: datetime, market_tz: str) -> str:
    """The broker-local date a UTC instant falls on."""
    return to_utc(moment).astimezone(ZoneInfo(market_tz)).date().isoformat()


def previous_market_date(market_tz: str, *, now: datetime) -> str:
    """The most recently completed broker day.

    A review is generated for the day that has **ended**: today is still accumulating, and
    a review of a partial day would be indistinguishable from a final one.
    """
    today = to_utc(now).astimezone(ZoneInfo(market_tz)).date()
    return (today - timedelta(days=1)).isoformat()


# ISO weekday of Friday, the last trading day of the broker week (§63).
FRIDAY = 5


def previous_iso_week(market_tz: str, *, now: datetime) -> tuple[int, int]:
    """The most recently **finished trading** week (§63).

    Not "seven days ago". A trading week ends at Friday's close, not at Sunday midnight, so
    the review the operator wants on a Saturday is the week that has just traded. Subtracting
    seven days returns the week *before* that one on Friday, Saturday and Sunday -- so a cron
    firing "after Friday's close", which is exactly what §63 asks for, would report last
    week's numbers under this week's heading, and nothing in the document would say so.

    So: take the most recently completed broker day, then walk back to the most recent Friday
    at or before it, and return that Friday's ISO week. Correct on every weekday -- from
    Saturday through the following Friday it names the same week, and it never names a week
    that is still trading.
    """
    today = to_utc(now).astimezone(ZoneInfo(market_tz)).date()
    # Yesterday: today is still accumulating, exactly as for a daily review.
    cursor = today - timedelta(days=1)
    cursor -= timedelta(days=(cursor.isoweekday() - FRIDAY) % 7)
    iso = cursor.isocalendar()
    return iso.year, iso.week

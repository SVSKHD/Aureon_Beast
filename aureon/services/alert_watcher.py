"""Firing ``/remind`` alerts from the quotes the observer already reads (9C).

The observer owns this for one reason: it is the process with quotes. Discord may not call the
broker (CLAUDE.md), and adding a second MT5 connection to poll levels would double the
terminal's load and introduce a clock nobody else shares. So the observer, on each poll, hands
its quote to ``check`` and this answers every armed alert that the quote crossed.

## What is frozen, and why the observer freezes it

An alert's message is only worth reading if it describes **the market that crossed the level**.
Discord renders the stored snapshot rather than reading state a few seconds later -- an
interval in which the cross it is describing may already have reversed. Freezing needs the
indicators, and the observer has them; Discord does not, and may not compute them.

The snapshot's shape is the ``/status`` panel's, deliberately: a human reading the reminder and
a human reading `/status` should be comparing the same fields, and reusing
``MarketSnapshot.as_state`` plus 9B's context means there is one definition of "what the market
looked like" rather than two that drift.

## Exactly once, and never from a stale read

``fire`` is transactional and refuses anything that is not still ARMED (9C-1), so a second
observer instance -- or a cancel that landed a moment earlier -- loses without a message being
sent. This module therefore does not need to be the only caller, which is the property that
makes it safe to run two observers during a deploy.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from aureon.models.alerts import PriceAlert
from aureon.models.base import to_utc, utc_now
from aureon.models.market import QuoteSnapshot
from aureon.models.profile import ProfileSummary, VolatilityContext
from aureon.storage.alert_repository import AlertClaimRejected, PriceAlertRepository

log = logging.getLogger(__name__)


@dataclass
class FiredAlert:
    """One answered alert, for the caller's log and the tests."""

    alert_id: str
    symbol: str
    level: float
    price: float


def build_snapshot(
    *,
    quote: QuoteSnapshot | None,
    state_fields: dict[str, object],
    profiles: dict[str, ProfileSummary] | None = None,
    volatility: VolatilityContext | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """What the market looked like when the level was crossed (9C).

    Built from the same fields ``/status`` renders -- ``MarketSnapshot.as_state`` plus 9B's
    profile summaries and volatility -- so the reminder and the panel cannot describe the same
    moment differently.

    ``minutes_since_cross`` is computed here rather than stored, because it is the only field
    whose value depends on *when the alert fired* rather than on what the market did. Storing a
    stale "4 minutes ago" would be worse than storing nothing.
    """
    moment = to_utc(now or utc_now())
    snapshot: dict[str, object] = {
        "bid": None if quote is None else quote.bid,
        "ask": None if quote is None else quote.ask,
        "captured_at": None if quote is None else to_utc(quote.captured_at).isoformat(),
    }

    for key in (
        "ema_fast",
        "ema_slow",
        "ema_distance",
        "rsi",
        "rsi_zone",
        "session",
        "session_trend",
        "session_high",
        "session_low",
        "last_cross",
        "last_sweep",
        "last_wick",
        "last_breakout",
        "detections_today",
    ):
        value = state_fields.get(key)
        snapshot[key] = getattr(value, "value", value)

    # Where price stands relative to the EMAs, named rather than left to a reader to
    # subtract: the sign of a distance is the bias, and a reminder is read in a hurry.
    fast, slow = snapshot.get("ema_fast"), snapshot.get("ema_slow")
    if isinstance(fast, (int, float)) and isinstance(slow, (int, float)):
        snapshot["ema_relation"] = "above" if fast >= slow else "below"
    else:
        snapshot["ema_relation"] = None

    last_cross_at = state_fields.get("last_cross_at")
    snapshot["minutes_since_cross"] = minutes_since(last_cross_at, moment)

    if profiles:
        snapshot["volume_profile"] = {
            scope: summary.model_dump(mode="json") for scope, summary in profiles.items()
        }
    if volatility is not None:
        snapshot["volatility"] = volatility.model_dump(mode="json")
    return snapshot


def minutes_since(moment: object, now: datetime) -> float | None:
    """Minutes from ``moment`` to ``now``, or ``None`` if there is no usable moment.

    Public because 9D's trend read needs the same figure for the same field, and two
    implementations of "how long since the last cross" would eventually disagree in a
    reminder and a readout describing the same instant.
    """
    if moment is None:
        return None
    if isinstance(moment, str):
        from datetime import datetime as _dt

        try:
            moment = _dt.fromisoformat(moment)
        except ValueError:
            return None
    if not isinstance(moment, datetime):
        return None
    return round((now - to_utc(moment)).total_seconds() / 60.0, 1)


class AlertWatcher:
    """Answers armed alerts from the quotes it is handed (9C)."""

    def __init__(self, alerts: PriceAlertRepository) -> None:
        self.alerts = alerts
        self.fired = 0
        self.expired = 0

    def check(
        self,
        symbol: str,
        quote: QuoteSnapshot | None,
        *,
        snapshot: dict[str, object] | None = None,
        now: datetime | None = None,
    ) -> list[FiredAlert]:
        """Fire every armed alert on ``symbol`` that this quote crossed.

        A missing quote answers nothing, rather than treating "no price" as a price. The
        snapshot is passed in by the caller because the observer is what knows the
        indicators; this module only decides *whether* the level was crossed.
        """
        if quote is None:
            return []
        moment = to_utc(now or utc_now())
        answered: list[FiredAlert] = []
        for alert in self._armed_for(symbol):
            if not alert.crossed_by(quote.bid, quote.ask):
                continue
            price = quote.ask if alert.side == "above" else quote.bid
            try:
                self.alerts.fire(
                    alert.alert_id,
                    price=price,
                    snapshot=dict(snapshot or {}),
                    now=moment,
                )
            except AlertClaimRejected as exc:
                # Cancelled, expired, or answered by another instance between the read and
                # the write. Nothing to send and nothing to repair.
                log.info("alert %s not fired: %s", alert.alert_id, exc)
                continue
            self.fired += 1
            answered.append(
                FiredAlert(
                    alert_id=alert.alert_id,
                    symbol=alert.symbol,
                    level=alert.level,
                    price=price,
                )
            )
            log.info(
                "alert %s fired: %s %s %s at %s",
                alert.alert_id,
                alert.symbol,
                alert.side,
                alert.level,
                price,
            )
        return answered

    def _armed_for(self, symbol: str) -> Sequence[PriceAlert]:
        try:
            return self.alerts.armed(symbol=symbol)
        except Exception:  # noqa: BLE001 - alerting must never stop observation
            log.exception("could not read armed alerts for %s", symbol)
            return []

    def expire(self, *, now: datetime | None = None) -> list[str]:
        """Expire alerts past their window. Called on candle close (9C).

        On the candle clock rather than a timer, so expiry happens on the same clock as
        everything else the observer records and needs no second scheduler.
        """
        try:
            expired = self.alerts.expire_due(now=now)
        except Exception:  # noqa: BLE001 - same reason as above
            log.exception("could not expire alerts")
            return []
        self.expired += len(expired)
        if expired:
            log.info("expired %d alert(s): %s", len(expired), ", ".join(expired))
        return expired

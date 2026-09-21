"""Discord over the weekend (11B).

Discord is the one service that does not decide whether the market is open. It has no feed
to classify and may not call the broker (CLAUDE.md), so it **reads** the phase the observer
publishes in ``system_state``. Everything here follows from that:

* ``/status`` says "closed — next open Sun 20 Sep 22:00 UTC" rather than just "closed",
  because "closed" alone reads as a fault and the first thing somebody does about a fault
  is restart something;
* the freshness thresholds widen while the services are asleep. Without this, four services
  beating every five minutes against a forty-five second threshold read as four dead
  services every single weekend -- and a screen that cries wolf weekly is a screen nobody
  reads on the Monday it is right;
* ``/execute`` is **refused**, where a merely-CLOSED symbol row is only a warning. The
  difference is the confirmation TTL: a stale feed may clear inside a minute, a weekend will
  not, so the only thing a confirmation could do is expire;
* ``/remind`` still arms. A level somebody wants to know about on Monday is exactly the sort
  of thing to set up on a Saturday.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from aureon.discord.service import (
    NO_REVIEW_YET,
    build_status,
    discord_cadences,
    plan_market_order,
    plan_price_alert,
    weekend_notice,
)
from aureon.execution.fake_broker import DEFAULT_SYMBOL_INFO
from aureon.models.enums import (
    Freshness,
    MarketState,
    SleepPhase,
    Timeframe,
)
from aureon.models.market import QuoteSnapshot
from aureon.models.settings import ExecutionSettings
from aureon.models.system import Heartbeat, SymbolState, SystemState

SYMBOL = "XAUUSD"
NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)  # a Saturday
SUNDAY_OPEN = datetime(2026, 9, 20, 22, 0, tzinfo=UTC)

#: 300s asleep, so the widened threshold is 600s.
SLEEP_HEARTBEAT = 300.0


def settings(**overrides) -> ExecutionSettings:
    base = dict(trading_enabled=True, max_lot=1.0, status_stale_after_seconds=45.0)
    return ExecutionSettings(**(base | overrides))


def asleep_state(
    *,
    phase: SleepPhase = SleepPhase.ASLEEP,
    next_open: datetime | None = SUNDAY_OPEN,
    updated_at: datetime = NOW,
    market_state: MarketState = MarketState.CLOSED,
) -> SystemState:
    return SystemState(
        updated_at=updated_at,
        symbols=(
            SymbolState(symbol=SYMBOL, timeframe=Timeframe.M5, market_state=market_state),
        ),
        sleep_phase=phase,
        next_market_open=next_open,
    )


def status(state: SystemState | None, **overrides):
    base = dict(
        system_state=state,
        heartbeats={},
        settings=settings(),
        now=NOW,
        sleep_heartbeat_seconds=SLEEP_HEARTBEAT,
    )
    return build_status(**(base | overrides))


# ── /status says closed, and when it opens ───────────────────────────────────


def test_the_screen_names_the_next_open() -> None:
    screen = status(asleep_state())
    assert screen.asleep
    assert screen.closed_line == "closed (asleep) — next open Sun 20 Sep 22:00 UTC"


def test_an_open_market_has_no_closed_line() -> None:
    """A "next open" on a Tuesday afternoon reads as a closure that is not happening."""
    screen = status(
        asleep_state(
            phase=SleepPhase.AWAKE, next_open=None, market_state=MarketState.OPEN
        )
    )
    assert not screen.asleep
    assert screen.closed_line is None


def test_a_waking_service_is_still_shut() -> None:
    """WAKING means the loops are running again, not that a trade may execute."""
    screen = status(asleep_state(phase=SleepPhase.WAKING))
    assert screen.asleep
    assert screen.market_closed
    assert "waking" in (screen.closed_line or "")


def test_a_published_phase_beats_the_symbol_row() -> None:
    """The row is whatever the market-state service last said; the phase is what the
    observer acted on."""
    screen = status(asleep_state(market_state=MarketState.OPEN))
    assert screen.market_closed is True
    assert screen.review_summary == NO_REVIEW_YET


def test_a_missing_next_open_says_so_rather_than_inventing_one() -> None:
    screen = status(asleep_state(next_open=None))
    assert screen.closed_line == "closed (asleep) — next open unknown"


def test_the_closed_line_is_rendered_above_the_symbol_rows() -> None:
    """Through the embed, not just the screen: a line no renderer emits is a line nobody
    reads, and that has been a recorded failure mode twice (decisions 188, 204)."""
    from aureon.discord.embeds import status_embed

    embed = status_embed(status(asleep_state()))
    market = next(f for f in embed.fields if f.name == "Market")
    assert "next open Sun 20 Sep 22:00 UTC" in market.value
    assert market.value.index("next open") < market.value.index(SYMBOL)


# ── The hang detector widens ─────────────────────────────────────────────────


def beats(age_seconds: float) -> dict[str, Heartbeat]:
    at = NOW - __import__("datetime").timedelta(seconds=age_seconds)
    return {
        name: Heartbeat(service=name, instance_id="i", updated_at=at)
        for name in ("observer", "executor", "monitor", "discord")
    }


def test_a_five_minute_heartbeat_is_live_while_the_services_are_asleep() -> None:
    """The one that makes a weekend readable.

    At 45 seconds -- the awake threshold -- every service beating every five minutes is
    STALE from the first weekend minute onward.
    """
    screen = status(asleep_state(), heartbeats=beats(305))
    assert [s.freshness for s in screen.services] == [Freshness.LIVE] * 4


def test_the_same_heartbeat_is_stale_while_awake() -> None:
    """The control. Without it the test above would pass on a threshold that had simply
    been widened for ever."""
    awake = asleep_state(
        phase=SleepPhase.AWAKE, next_open=None, market_state=MarketState.OPEN
    )
    screen = status(awake, heartbeats=beats(305))
    assert all(s.freshness is not Freshness.LIVE for s in screen.services)


def test_two_missed_sleep_beats_is_still_a_hang() -> None:
    """Widened to twice the sleep cadence, not disabled: one missed beat is a gap, two is a
    process that died over the weekend, and that is what an operator needs to hear."""
    screen = status(asleep_state(), heartbeats=beats(2 * SLEEP_HEARTBEAT + 60))
    assert all(s.freshness is not Freshness.LIVE for s in screen.services)


def test_the_widened_threshold_never_narrows_a_configured_one() -> None:
    """A deployment that already tolerates twenty minutes keeps tolerating it."""
    screen = status(
        asleep_state(),
        settings=settings(status_stale_after_seconds=1200.0),
        heartbeats=beats(900),
    )
    assert [s.freshness for s in screen.services] == [Freshness.LIVE] * 4


# ── /execute is refused, /remind is not ──────────────────────────────────────


def quote() -> QuoteSnapshot:
    return QuoteSnapshot(
        symbol=SYMBOL, bid=2400.0, ask=2400.3, point=0.01, captured_at=NOW
    )


def test_an_order_is_refused_while_the_market_is_shut() -> None:
    plan = plan_market_order(
        symbol=SYMBOL,
        side="buy",
        lot="0.10",
        requested_by="user-1",
        settings=settings(),
        info=DEFAULT_SYMBOL_INFO,
        observed_symbols=(SYMBOL,),
        system_state=asleep_state(),
    )
    assert not plan.ok
    assert plan.draft is None, "nothing is left behind for the executor to find"
    assert "closed" in (plan.message or "")
    assert "Sun 20 Sep 22:00" in (plan.message or "")


def test_the_same_order_is_allowed_when_the_market_is_open() -> None:
    plan = plan_market_order(
        symbol=SYMBOL,
        side="buy",
        lot="0.10",
        requested_by="user-1",
        settings=settings(),
        info=DEFAULT_SYMBOL_INFO,
        observed_symbols=(SYMBOL,),
        system_state=asleep_state(
            phase=SleepPhase.AWAKE, next_open=None, market_state=MarketState.OPEN
        ),
    )
    assert plan.ok and plan.draft is not None


def test_an_order_with_no_published_state_is_not_refused_for_that_reason() -> None:
    """A missing state document is already reported by ``/status`` and stops the order a
    step later, for a reason that names the observer. Refusing it here as "the market is
    closed" would be a guess dressed as a fact."""
    plan = plan_market_order(
        symbol=SYMBOL,
        side="buy",
        lot="0.10",
        requested_by="user-1",
        settings=settings(),
        info=DEFAULT_SYMBOL_INFO,
        observed_symbols=(SYMBOL,),
        system_state=None,
    )
    assert plan.ok


@pytest.mark.parametrize("phase", [SleepPhase.ASLEEP, SleepPhase.WAKING])
def test_the_refusal_names_what_still_works(phase: SleepPhase) -> None:
    notice = weekend_notice(asleep_state(phase=phase))
    assert notice is not None
    assert "/remind" in notice and "/monitor" in notice


def test_a_reminder_is_still_armed_over_the_weekend() -> None:
    """A level somebody wants to know about on Monday is exactly the sort of thing to set
    up on a Saturday. Nothing about arming one needs the market open: the observer checks
    it when quotes resume."""
    plan = plan_price_alert(
        symbol=SYMBOL,
        side="above",
        level=2450.0,
        quote=quote(),
        requested_by="user-1",
        observed_symbols=(SYMBOL,),
        armed_count=0,
    )
    assert plan.ok, plan.message


# ── The process's own cadences ───────────────────────────────────────────────


CADENCES = dict(
    awake_heartbeat=15.0, awake_poll=5.0, sleep_heartbeat=300.0, sleep_poll=60.0
)


def test_the_bot_slows_down_when_the_observer_says_it_is_shut() -> None:
    cadences = discord_cadences(asleep_state(), **CADENCES)
    assert (cadences.heartbeat_seconds, cadences.poll_seconds) == (300.0, 60.0)


def test_the_bot_speeds_up_at_the_open() -> None:
    awake = asleep_state(
        phase=SleepPhase.AWAKE, next_open=None, market_state=MarketState.OPEN
    )
    cadences = discord_cadences(awake, **CADENCES)
    assert (cadences.heartbeat_seconds, cadences.poll_seconds) == (15.0, 5.0)


def test_a_missing_state_document_leaves_the_bot_at_the_awake_cadence() -> None:
    """Following nothing is not a reason to go quiet: a bot that slowed itself down because
    it could not read Firestore would answer slowly at exactly the moment somebody was
    trying to find out why."""
    cadences = discord_cadences(None, **CADENCES)
    assert (cadences.heartbeat_seconds, cadences.poll_seconds) == (15.0, 5.0)

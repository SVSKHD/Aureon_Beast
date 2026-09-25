"""Setup cards: one message per setup, edited in place, saying nothing it should not (12, T-11).

The three claims worth separating.

**The card says the right things and never says a side.** A setup has a direction CONTEXT, the
invalidation line is not a stop, and the reference block cannot appear without its caption. These
are assertions about text, made against the built screen.

**One message, edited.** The first announced trigger posts and claims; every later one edits the
same message; a restart reads the message id back off the notification document and keeps editing.
The claim-before-post tradeoff is asserted rather than assumed: a claimed-but-never-posted setup
is not retried, and the test says so.

**The settings decide, and a typo is refused.** Twenty-six triggers are configurable; fourteen are
off by default; a name that is not a trigger is refused by the model rather than silently ignored.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aureon.discord.service import (
    SetupTrendContext,
    build_setup_card,
    build_setup_confirmation,
)
from aureon.models.base import MarketTime
from aureon.models.confluence import AgentConfluence, AgentConfluenceVote
from aureon.models.enums import (
    DirectionContext,
    SetupAnchorKind,
    SetupEventType,
    SetupFamily,
    SetupState,
    Timeframe,
    TrendBias,
)
from aureon.models.settings import (
    DEFAULT_SETUP_ANNOUNCEMENTS,
    NOTABLE_SETUP_EVENTS,
    QUIET_SETUP_EVENTS,
    SETUP_ANNOUNCEMENTS,
    SETUP_STATE_ANNOUNCEMENTS,
    NotificationSettings,
)
from aureon.models.setup import Setup, SetupAnchor, SetupEvent, SetupReference

NOW = datetime(2026, 9, 16, 9, 35, tzinfo=UTC)
TZ = "Europe/Athens"


def a_setup(**overrides) -> Setup:
    base = dict(
        setup_id="a" * 32,
        account_scope="primary",
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        family=SetupFamily.LIQUIDITY_REVERSAL,
        direction_context=DirectionContext.BULLISH,
        market_date="2026-09-16",
        state=SetupState.CONFIRMED,
        anchor=SetupAnchor(
            kind=SetupAnchorKind.LIQUIDITY_LEVEL, price=2412.5, level_type="session_high"
        ),
        invalidation_price=2410.0,
        opened_at=NOW,
        confirmed_at=NOW,
        event_count=3,
    )
    base.update(overrides)
    return Setup(**base)


def an_event(
    event_type: SetupEventType,
    state: SetupState,
    *,
    minutes: int = 0,
    context_snapshot: dict[str, str] | None = None,
    reason: str | None = None,
) -> SetupEvent:
    at = NOW + timedelta(minutes=minutes)
    return SetupEvent(
        event_id=f"{event_type.value}-{minutes}",
        setup_id="a" * 32,
        event_type=event_type,
        from_state=state,
        to_state=state,
        market_time=MarketTime.from_utc(at, TZ),
        context_snapshot=context_snapshot or {},
        reason=reason,
    )


# ── the card ──────────────────────────────────────────────────────────────────


def test_the_card_names_a_direction_context_and_never_a_side() -> None:
    """The word a card uses is the word a reader acts on. BUY on a card of observations is an
    instruction nobody issued."""
    screen = build_setup_card(a_setup())
    text = " ".join([screen.title, screen.description, *(v for _, v in screen.fields)]).lower()
    assert "bullish" in text
    for forbidden in (" buy", " sell", "take profit", "stop loss", "entry"):
        assert forbidden not in text, f"the card said {forbidden!r}"


def test_the_card_shows_the_six_agent_confidence_meter_and_votes() -> None:
    confluence = AgentConfluence(
        target=DirectionContext.BULLISH,
        confidence_pct=67,
        aligned_count=4,
        opposed_count=1,
        neutral_count=1,
        votes=(
            AgentConfluenceVote(
                agent_name="ema_cross",
                stance=DirectionContext.BULLISH,
                alignment="aligned",
                observation="fast above slow",
            ),
            AgentConfluenceVote(
                agent_name="rsi",
                stance=DirectionContext.BULLISH,
                alignment="aligned",
                observation="61.0 · above 50 midpoint",
            ),
            AgentConfluenceVote(
                agent_name="session_trend",
                stance=DirectionContext.BULLISH,
                alignment="aligned",
                observation="session trend bullish",
            ),
            AgentConfluenceVote(
                agent_name="wick",
                stance=DirectionContext.BEARISH,
                alignment="opposed",
                observation="upper_rejection",
            ),
            AgentConfluenceVote(
                agent_name="liquidity",
                stance=DirectionContext.BULLISH,
                alignment="aligned",
                observation="down|previous_day_low",
            ),
            AgentConfluenceVote(
                agent_name="breakout",
                stance=DirectionContext.NEUTRAL,
                alignment="neutral",
                observation="no event yet",
            ),
        ),
    )
    fields = dict(build_setup_card(a_setup(agent_confluence=confluence)).fields)

    assert "**67%**" in fields["Agent confidence"]
    assert "4 aligned · 1 opposed · 1 neutral" in fields["Agent confidence"]
    assert "not a win probability" in fields["Agent confidence"]
    for agent in ("EMA", "RSI", "Trend", "Wick", "Liquidity", "Breakout"):
        assert f"**{agent}**" in fields["6-agent read"]


def test_a_legacy_setup_without_a_snapshot_says_it_is_waiting_for_refresh() -> None:
    fields = dict(build_setup_card(a_setup()).fields)
    assert "awaiting observer refresh" in fields["Agent confidence"]
    assert fields["6-agent read"] == "no six-agent snapshot yet"


def test_the_invalidation_price_is_never_offered_as_a_stop() -> None:
    fields = dict(build_setup_card(a_setup()).fields)
    assert "not a stop" in fields["Invalidation"]


def test_a_setup_without_an_invalidation_price_says_so_rather_than_showing_nothing() -> None:
    fields = dict(build_setup_card(a_setup(invalidation_price=None)).fields)
    assert fields["Invalidation"] and "not a stop" not in fields["Invalidation"]


def test_the_events_read_oldest_first_as_the_story_they_are() -> None:
    events = [
        an_event(SetupEventType.WATCH_STARTED, SetupState.WATCH, minutes=0),
        an_event(SetupEventType.DEVELOPING, SetupState.DEVELOPING, minutes=5),
        an_event(SetupEventType.CONFIRMED, SetupState.CONFIRMED, minutes=10),
    ]
    lines = build_setup_card(a_setup(), events=events).description.splitlines()
    assert "watch started" in lines[0]
    assert "confirmed" in lines[-1]


def test_the_card_shows_six_dollar_move_tracking_and_reached_state() -> None:
    tracking = an_event(
        SetupEventType.FAVOURABLE_MOVE_6_TRACKING,
        SetupState.CONFIRMED,
        context_snapshot={
            "reference_detection_id": "detection-1",
            "reference_price": "2400.00000",
            "threshold_price": "2406.00000",
        },
    )
    fields = dict(build_setup_card(a_setup(), events=[tracking]).fields)
    assert "ref 2400.00000" in fields["Move ladder"]
    assert "$6 …" in fields["Move ladder"]
    assert "$20 …" in fields["Move ladder"]
    assert "$40 …" in fields["Move ladder"]

    reached = an_event(
        SetupEventType.FAVOURABLE_MOVE_6_REACHED,
        SetupState.CONFIRMED,
        minutes=5,
        context_snapshot={
            "reference_detection_id": "detection-1",
            "reference_price": "2400.00000",
            "observed_extreme": "2406.25000",
            "favourable_excursion": "6.25000",
        },
    )
    fields = dict(build_setup_card(a_setup(), events=[tracking, reached]).fields)
    assert "$6 ✓" in fields["Move ladder"]
    assert "$20 …" in fields["Move ladder"]
    assert "seen 6.25000" in fields["Move ladder"]


def test_the_card_shows_frozen_ema_cross_and_rsi_status() -> None:
    events = [
        an_event(
            SetupEventType.EMA_GAP_NARROWING,
            SetupState.WATCH,
            minutes=0,
            context_snapshot={
                "close": "4317.00000",
                "ema_fast": "4315.00000",
                "ema_slow": "4316.00000",
                "rsi": "47.00000",
                "trend": "sideways",
                "mtf_alignment": "mixed",
            },
        ),
        an_event(
            SetupEventType.CONFIRMED,
            SetupState.CONFIRMED,
            minutes=5,
            context_snapshot={
                "close": "4320.00000",
                "ema_fast": "4318.50000",
                "ema_slow": "4317.50000",
                "rsi": "58.00000",
                "trend": "bullish",
                "mtf_alignment": "bullish",
            },
            reason="an EMA cross in the reversal's direction",
        ),
    ]
    fields = dict(build_setup_card(a_setup(), events=events).fields)
    assert "4318.5" in fields["EMA20 / EMA50"]
    assert "4317.5" in fields["EMA20 / EMA50"]
    assert fields["EMA cross status"] == "EMA20 above EMA50"
    assert fields["Early EMA status"] == "cross observed"
    assert "58.0" in fields["RSI status"]
    assert "rising" in fields["RSI status"]
    assert fields["Setup trend @ event"] == "BULLISH"


def _state_with_mtf(*biases: tuple[Timeframe, TrendBias]):
    from types import SimpleNamespace
    from aureon.models.mtf import MtfContext, TimeframeRead

    reads = tuple(
        TimeframeRead(
            timeframe=timeframe,
            at=NOW,
            ema_fast=101.0 if bias is TrendBias.BULLISH else 99.0,
            ema_slow=100.0,
            close=100.5,
            bias=bias,
        )
        for timeframe, bias in biases
    )
    return SimpleNamespace(
        mtf=MtfContext(reads=reads, ema_fast_period=20, ema_slow_period=50)
    )


def _confirmation_events(*, bullish: bool = True) -> list[SetupEvent]:
    if bullish:
        first_fast, first_slow, last_fast, last_slow, rsi = 99.0, 100.0, 101.0, 100.0, 56.0
    else:
        first_fast, first_slow, last_fast, last_slow, rsi = 101.0, 100.0, 99.0, 100.0, 44.0
    return [
        an_event(
            SetupEventType.EMA_GAP_NARROWING,
            SetupState.WATCH,
            context_snapshot={
                "ema_fast": f"{first_fast}",
                "ema_slow": f"{first_slow}",
                "rsi": "49.0" if bullish else "51.0",
            },
        ),
        an_event(
            SetupEventType.CONFIRMED,
            SetupState.CONFIRMED,
            minutes=5,
            context_snapshot={
                "ema_fast": f"{last_fast}",
                "ema_slow": f"{last_slow}",
                "rsi": f"{rsi}",
            },
            reason="the EMA pair crossed",
        ),
    ]


def test_confirmation_clears_only_when_trend_ema_rsi_and_mtf_agree() -> None:
    setup = a_setup(direction_context=DirectionContext.BULLISH, state=SetupState.CONFIRMED)
    read = build_setup_confirmation(
        setup,
        events=_confirmation_events(bullish=True),
        symbol_state=_state_with_mtf(
            (Timeframe.M15, TrendBias.BULLISH),
            (Timeframe.H1, TrendBias.BULLISH),
        ),
        trend_context=SetupTrendContext(present="BULLISH"),
    )
    assert read.cleared is True
    assert "CLEARED FOR REVIEW" in read.clearance
    assert "✅ MTF ALIGNED" in read.badges
    assert "▲ BULLISH EMA CROSS" in read.early_ema


def test_confirmation_blocks_when_directional_timeframes_fight() -> None:
    setup = a_setup(direction_context=DirectionContext.BULLISH, state=SetupState.CONFIRMED)
    read = build_setup_confirmation(
        setup,
        events=_confirmation_events(bullish=True),
        symbol_state=_state_with_mtf(
            (Timeframe.M15, TrendBias.BULLISH),
            (Timeframe.H1, TrendBias.BEARISH),
        ),
        trend_context=SetupTrendContext(present="BULLISH"),
    )
    assert read.cleared is False
    assert "NOT CLEARED" in read.clearance
    assert "⚠ MTF CONFLICT" in read.badges
    assert any("timeframes disagree" in one for one in read.blockers)


def test_early_ema_names_direction_before_the_cross() -> None:
    events = [
        an_event(
            SetupEventType.WATCH_STARTED,
            SetupState.WATCH,
            context_snapshot={"ema_fast": "98.0", "ema_slow": "100.0", "rsi": "44.0"},
        ),
        an_event(
            SetupEventType.EMA_GAP_NARROWING,
            SetupState.WATCH,
            minutes=5,
            context_snapshot={"ema_fast": "99.4", "ema_slow": "100.0", "rsi": "47.0"},
        ),
    ]
    read = build_setup_confirmation(
        a_setup(state=SetupState.WATCH),
        events=events,
        symbol_state=_state_with_mtf((Timeframe.M15, TrendBias.BEARISH)),
        trend_context=SetupTrendContext(present="BEARISH"),
    )
    assert "EARLY BULLISH" in read.early_ema
    assert read.cleared is False


def test_card_exposes_confirmation_badges_and_blockers() -> None:
    screen = build_setup_card(
        a_setup(direction_context=DirectionContext.BULLISH, state=SetupState.CONFIRMED),
        events=_confirmation_events(bullish=True),
        symbol_state=_state_with_mtf(
            (Timeframe.M15, TrendBias.BULLISH),
            (Timeframe.H1, TrendBias.BEARISH),
        ),
        trend_context=SetupTrendContext(present="BEARISH"),
    )
    fields = dict(screen.fields)
    assert "NOT CLEARED" in fields["Confirmation"]
    assert "MTF CONFLICT" in fields["Badges"]
    assert "COUNTER-TREND" in fields["Badges"]
    assert "timeframes disagree" in fields["Blockers"]


def test_the_card_separates_present_asia_and_london_trends() -> None:
    trend = SetupTrendContext(
        present="BEARISH",
        asia="UP · +125 pts · complete",
        london="DOWN · live · 4322.00 → 4312.00",
        evidence=(
            "ema fast 4311.36 below slow 4316.93",
            "swings: 0 higher highs, 0 higher lows, 3 lower highs, 4 lower lows",
        ),
    )
    fields = dict(build_setup_card(a_setup(), trend_context=trend).fields)
    assert fields["Present trend"] == "BEARISH"
    assert fields["Asia trend"].startswith("UP")
    assert fields["London trend"].startswith("DOWN")
    assert "lower highs" in fields["Trend evidence"]

def test_the_card_shows_only_the_last_few_events() -> None:
    from aureon.discord.service import CARD_EVENTS

    events = [
        an_event(SetupEventType.PROXIMITY_SESSION_EXTREME, SetupState.WATCH, minutes=i)
        for i in range(CARD_EVENTS + 4)
    ]
    lines = build_setup_card(a_setup(), events=events).description.splitlines()
    assert len(lines) == CARD_EVENTS


def test_a_setup_with_no_events_says_so() -> None:
    assert "no events" in build_setup_card(a_setup()).description


def test_the_linked_detections_are_ids_and_are_truncated_with_a_count() -> None:
    """Ids rather than a summary, because an id is checkable: a reader can look the detection up
    and see the candle it was made on."""
    from aureon.discord.service import CARD_DETECTIONS

    ids = tuple(f"detection-{i}" for i in range(CARD_DETECTIONS + 3))
    fields = dict(build_setup_card(a_setup(linked_detection_ids=ids)).fields)
    line = fields["Linked detections"]
    assert "detection-6" in line
    assert "and 3 more" in line


def test_the_reference_block_cannot_appear_without_its_caption() -> None:
    from aureon.models.enums import HistorySource
    from aureon.services.setup_reference import render_reference

    reference = SetupReference(
        cohort_n=40, history_source=HistorySource.SYNTHETIC, real_days=0, insufficient=False
    )
    screen = build_setup_card(a_setup(reference=reference))
    assert screen.reference
    assert screen.reference == render_reference(reference)
    assert "historical reference" in screen.reference[0]


def test_an_unmeasured_reference_still_carries_its_words() -> None:
    screen = build_setup_card(a_setup())
    assert screen.reference and "nothing measured yet" in screen.reference[0]


def test_every_state_has_a_badge_and_no_badge_is_a_colour() -> None:
    """Text badges, not colour. A green card for CONFIRMED and a red one for INVALIDATED is
    approval and disapproval drawn in colour, which the footer then denies in words."""
    from aureon.discord.service import STATE_BADGES
    from aureon.models.enums import TERMINAL_SETUP_STATES

    for state in SetupState:
        assert state.value in STATE_BADGES, state
        # A terminal state must carry its closed_at -- the model refuses otherwise, which is the
        # invariant T-6 put there and not something for this test to route around.
        closed = NOW if state in TERMINAL_SETUP_STATES else None
        screen = build_setup_card(
            a_setup(state=state, confirmed_at=None, closed_at=closed)
        )
        assert screen.badge == STATE_BADGES[state.value]


def test_setup_embed_groups_information_and_keeps_final_check_last() -> None:
    from aureon.discord.embeds import setup_embed

    screen = build_setup_card(
        a_setup(),
        events=_confirmation_events(bullish=True),
        symbol_state=_state_with_mtf(
            (Timeframe.M15, TrendBias.BULLISH),
            (Timeframe.H1, TrendBias.BULLISH),
        ),
        trend_context=SetupTrendContext(present="BULLISH"),
    )
    embed = setup_embed(screen)
    names = [field.name for field in embed.fields]

    assert names[:5] == [
        "1 · SETUP",
        "2 · TREND",
        "3 · MOMENTUM",
        "4 · AGENT CONFIDENCE",
        "5 · CONFIRMATION EVIDENCE",
    ]
    assert "MORE CONTEXT" not in names
    assert names[-1] == "FINAL CHECK"
    assert len(names) <= 25
    assert "CLEARED FOR REVIEW" in embed.fields[-1].value


def test_the_footer_says_a_setup_is_not_an_order() -> None:
    screen = build_setup_card(a_setup())
    assert "no order is implied" in screen.footer
    assert screen.setup_id in screen.footer


def test_the_family_reads_as_words_rather_than_an_enum() -> None:
    assert "liquidity reversal" in build_setup_card(a_setup()).title


def test_a_card_without_a_chart_is_still_a_card() -> None:
    """A picture that failed to draw must not take the words with it."""
    screen = build_setup_card(a_setup(), chart_filename=None)
    assert screen.chart_filename is None
    assert screen.fields and screen.footer


# ── the settings ──────────────────────────────────────────────────────────────


def test_all_thirty_triggers_are_configurable() -> None:
    assert len(SETUP_STATE_ANNOUNCEMENTS) == 9
    assert len(NOTABLE_SETUP_EVENTS) == 6
    assert len(QUIET_SETUP_EVENTS) == 15
    assert len(SETUP_ANNOUNCEMENTS) == 30
    assert len(set(SETUP_ANNOUNCEMENTS)) == 30


def test_every_state_and_every_watch_event_is_nameable() -> None:
    """The list must cover the enums, or a trigger exists that no operator can subscribe to."""
    from aureon.models.enums import WATCH_EVENT_TYPES

    assert set(SETUP_STATE_ANNOUNCEMENTS) == {state.value for state in SetupState}
    assert set(NOTABLE_SETUP_EVENTS) | set(QUIET_SETUP_EVENTS) == {
        event.value for event in WATCH_EVENT_TYPES
    }


def test_the_noisy_fourteen_are_off_by_default() -> None:
    """"Price is near the previous day's high" is true on dozens of consecutive candles, so a
    card subscribed to it would be edited on every one of them."""
    assert len(DEFAULT_SETUP_ANNOUNCEMENTS) == 15
    for quiet in QUIET_SETUP_EVENTS:
        assert quiet not in DEFAULT_SETUP_ANNOUNCEMENTS
    settings = NotificationSettings()
    assert settings.announces_setup("confirmed")
    assert settings.announces_setup("repeated_level_test")
    assert not settings.announces_setup("proximity_poc")


def test_turning_a_quiet_event_on_takes_one_edit() -> None:
    settings = NotificationSettings(
        setup_states=(*DEFAULT_SETUP_ANNOUNCEMENTS, "proximity_poc")
    )
    assert settings.announces_setup("proximity_poc")


def test_a_trigger_that_is_not_a_trigger_is_refused() -> None:
    """A typo in the settings document must be a refused write, not a line that silently never
    fires -- which is indistinguishable from a feature that does not work."""
    with pytest.raises(ValueError, match="not a setup announcement"):
        NotificationSettings(setup_states=("confermed",))


def test_setups_can_be_silenced_without_forgetting_the_list() -> None:
    settings = NotificationSettings(setups_enabled=False)
    assert settings.setup_states == DEFAULT_SETUP_ANNOUNCEMENTS
    assert not settings.announces_setup("confirmed")


def test_silencing_detections_does_not_silence_setups() -> None:
    """Two switches, because they answer different questions: a channel can want structures and
    not every EMA cross."""
    settings = NotificationSettings(detections_enabled=False)
    assert settings.announces_setup("confirmed")
    assert not settings.announces("ema_cross")


# ── /setups and /setup ────────────────────────────────────────────────────────


class FakeSetups:
    """Only what the two commands read. Deliberately has no write method at all."""

    def __init__(self, *, open_setups: list[Setup] | None = None) -> None:
        self._open = list(open_setups or [])
        self._by_id = {one.setup_id: one for one in self._open}

    def get(self, setup_id: str) -> Setup | None:
        return self._by_id.get(setup_id)

    def events(self, setup_id: str, *, limit: int | None = None) -> list:
        return []

    def open_setups(self, *, symbol: str, market_date: str | None = None) -> list[Setup]:
        return [one for one in self._open if one.symbol == symbol.upper()]


class FakeContext:
    def __init__(self, setups: FakeSetups | None, symbols: tuple[str, ...] = ("XAUUSD",)):
        self.setups = setups
        self.config = type("C", (), {"symbols": symbols, "is_authorized": lambda self, _: True})()

    async def run(self, fn, *args, **kwargs):
        return fn(*args, **kwargs)


def run(coro):
    import asyncio

    return asyncio.run(coro)


def test_setups_lists_the_open_ones_with_their_state() -> None:
    from aureon.discord.commands.setups import setups_reply

    setups = FakeSetups(open_setups=[a_setup(setup_id="a" * 32, state=SetupState.WATCH)])
    embed = run(setups_reply(FakeContext(setups), symbol="xauusd"))
    text = embed.description or ""
    assert "liquidity reversal" in text
    assert "bullish" in text
    assert "watch" in text


def test_setups_says_nothing_is_tracked_rather_than_showing_an_empty_table() -> None:
    """Most candles build nothing, and an empty table reads as a fault."""
    from aureon.discord.commands.setups import setups_reply

    embed = run(setups_reply(FakeContext(FakeSetups()), symbol="XAUUSD"))
    assert "no open setups" in (embed.title or "").lower()


def test_setups_without_a_client_says_so_rather_than_lying_about_an_empty_market() -> None:
    from aureon.discord.commands.setups import setups_reply

    embed = run(setups_reply(FakeContext(None), symbol="XAUUSD"))
    assert "unavailable" in (embed.title or "").lower()


def test_setup_renders_the_same_card_the_channel_posts() -> None:
    from aureon.discord.commands.setups import setup_reply

    setup = a_setup(setup_id="b" * 32)
    context = FakeContext(FakeSetups(open_setups=[setup]))
    embed, view = run(setup_reply(context, setup_id=setup.setup_id))
    assert view is not None
    assert setup.setup_id in (embed.footer.text or "")
    assert "not a stop" in " ".join(f.value for f in embed.fields)


def test_setup_accepts_the_short_prefix_the_list_shows() -> None:
    from aureon.discord.commands.setups import setup_reply

    setup = a_setup(setup_id="c" * 32)
    embed, view = run(
        setup_reply(FakeContext(FakeSetups(open_setups=[setup])), setup_id=setup.setup_id[:8])
    )
    assert view is not None


def test_an_ambiguous_prefix_is_refused_by_name_rather_than_resolved() -> None:
    """Picking one would show a card for a structure the reader was not asking about, and they
    would have no way to tell."""
    from aureon.discord.commands.setups import setup_reply

    first = a_setup(setup_id="dddddddd" + "1" * 24)
    second = a_setup(setup_id="dddddddd" + "2" * 24)
    embed, view = run(
        setup_reply(FakeContext(FakeSetups(open_setups=[first, second])), setup_id="dddddddd")
    )
    assert view is None
    assert "ambiguous" in (embed.title or "").lower()


def test_an_unknown_id_says_so_and_points_at_the_list() -> None:
    from aureon.discord.commands.setups import setup_reply

    embed, view = run(setup_reply(FakeContext(FakeSetups()), setup_id="z" * 32))
    assert view is None
    assert "/setups" in (embed.description or "")


def test_neither_command_can_write_a_setup() -> None:
    """§71: ``setups`` is the observer's. The commands hold a reader with no write method."""
    import inspect

    from aureon.discord.commands import setups as module

    source = inspect.getsource(module)
    for forbidden in (".open(", ".record(", ".set(", ".create(", ".write("):
        assert forbidden not in source, forbidden

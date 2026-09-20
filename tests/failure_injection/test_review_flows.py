"""Reviews end to end against the emulator, and ``/status`` rendering one (§61-§65).

``tests/unit/test_reviews.py`` proves the arithmetic on synthetic data held in memory. What
it cannot prove is the part that only exists once Firestore is involved:

* a review **round-trips** -- a period written and read back reports the same figures, with
  tuples, enums and tz-aware timestamps surviving JSON in both directions;
* the period query files each document by the **market** clock, so a detection at 22:00 UTC
  on a Sunday counts towards Monday's review and not the previous week's;
* regenerating a period **overwrites the same document** rather than accumulating variants;
* ``/status`` on a CLOSED market shows the stored weekly review -- the Phase 7 gate -- and
  it reaches it through the read-only ``ReviewReader``, which cannot overwrite it.
"""

from __future__ import annotations

import asyncio

import pytest

from aureon.config import AureonConfig
from aureon.discord.context import build_context
from aureon.discord.service import NO_REVIEW_YET, build_status, summarise_review
from aureon.evaluation.rules import EMA_OUTCOME_V1, HORIZON_5_CANDLES
from aureon.models.base import MarketTime, utc_now
from aureon.models.enums import (
    MarketState,
    SessionName,
    Timeframe,
    TradeStatus,
)
from aureon.models.evaluation import DetectionEvaluation, HorizonResult
from aureon.models.session import SessionSummary
from aureon.models.settings import ExecutionSettings
from aureon.models.system import SymbolState, SystemState
from aureon.reviews.service import ReviewService
from aureon.storage.detection_repository import DetectionRepository
from aureon.storage.evaluation_repository import EvaluationRepository
from aureon.storage.review_reader import ReviewReader
from aureon.storage.session_repository import SessionRepository
from aureon.storage.trade_repository import TradeRepository
from tests.failure_injection.conftest import SYMBOL
from tests.unit.test_reviews import (
    ALL_REACHED,
    BASE,
    TZ,
    complete_evaluation,
    detection,
    trade,
)

pytestmark = pytest.mark.emulator

RULE = EMA_OUTCOME_V1
# BASE is Wednesday 2026-09-16 10:00 UTC — inside London, inside ISO week 38.
MARKET_DATE = "2026-09-16"
ISO = (2026, 38)


@pytest.fixture
def service(firestore_client) -> ReviewService:
    return ReviewService(
        firestore_client, RULE, market_tz=TZ, infer_window_minutes=30
    )


@pytest.fixture
def seed(firestore_client):
    """Write a period's documents through the repositories, as the services would."""
    detections = DetectionRepository(firestore_client)
    evaluations = EvaluationRepository(firestore_client)
    trades = TradeRepository(firestore_client, account_scope="primary")
    sessions = SessionRepository(firestore_client)

    def write(
        *,
        detection_list=(),
        evaluation_list=(),
        trade_list=(),
        session_list=(),
    ) -> None:
        for item in detection_list:
            detections.upsert(item)
        for item in evaluation_list:
            evaluations.upsert(item)
        for item in trade_list:
            stored = trades.upsert_open(item.model_copy(update={"status": TradeStatus.OPEN}))
            if item.status is not TradeStatus.OPEN:
                trades.transition(
                    stored.trade_id,
                    item.status,
                    updates={
                        "close_time": item.close_time,
                        "closed_volume": item.closed_volume,
                        "realized_pnl": item.realized_pnl,
                    },
                )
        for item in session_list:
            sessions.upsert(item)

    return write


def session_summary(session: SessionName = SessionName.LONDON) -> SessionSummary:
    started = BASE
    return SessionSummary(
        session_id=f"{MARKET_DATE}__{session.value}",
        account_scope="primary",
        symbol=SYMBOL,
        timeframe=Timeframe.M5,
        session=session,
        market_date=MARKET_DATE,
        session_config_version=1,
        started_at=MarketTime.from_utc(started, TZ),
        ended_at=MarketTime.from_utc(started.replace(hour=17), TZ),
        open=2400.0,
        high=2410.0,
        low=2395.0,
        close=2405.0,
        trend="up",
        change=5.0,
        change_points=500.0,
        range=15.0,
        candle_count=84,
    )


# ── Round trip ────────────────────────────────────────────────────────────────


def test_a_daily_review_round_trips_through_firestore(service, seed) -> None:
    """Figures written and read back must be the same figures.

    Not a formality: reviews hold tuples of nested models, enum-keyed dicts and tz-aware
    timestamps, and JSON has native forms for none of those.
    """
    seed(
        detection_list=[detection(ident="d1"), detection(ident="d2", minutes=45)],
        evaluation_list=[
            complete_evaluation("d1", reached=ALL_REACHED),
            complete_evaluation("d2", reached=ALL_REACHED),
        ],
        trade_list=[trade(ident="t1", minutes=5, pnl=120.0)],
        session_list=[session_summary()],
    )

    written = service.generate_daily(MARKET_DATE)
    read_back = ReviewReader(service._client).get_daily(MARKET_DATE)

    assert read_back is not None
    assert read_back.detections_total == written.detections_total == 2
    assert read_back.trades_total == 1
    assert read_back.trades_closed == 1
    assert read_back.realized_pnl == pytest.approx(120.0)
    assert read_back.detections_by_session == {SessionName.LONDON: 2}
    assert read_back.sessions_covered == (SessionName.LONDON,)
    assert read_back.period_start == written.period_start
    assert read_back.period_start.tzinfo is not None
    assert read_back.model_dump_json() == written.model_dump_json()


def test_reached_counts_survive_the_round_trip_from_complete_only(service, seed) -> None:
    """One COMPLETE and one PENDING detection: the denominator is 1, not 2.

    The unit suite proves this in memory. Proven again here because the PENDING horizon
    reaches Firestore as a stored document too, and a loader that treated a missing
    ``completed_at`` as complete would produce the same wrong denominator the phase exists
    to prevent -- with the honest-looking figure "2 of 2 reached".
    """
    pending = DetectionEvaluation(
        detection_id="d2",
        rule_id=RULE.rule_id,
        reference_price=RULE.reference_price,
        horizons=(HorizonResult(horizon_id=HORIZON_5_CANDLES),),
    )
    seed(
        detection_list=[detection(ident="d1"), detection(ident="d2", minutes=30)],
        evaluation_list=[complete_evaluation("d1", reached=ALL_REACHED), pending],
    )

    review = service.generate_daily(MARKET_DATE)
    outcome = next(h for h in review.horizons if h.horizon_id == HORIZON_5_CANDLES)
    first = outcome.thresholds[0]

    assert first.evaluated == 1, "a PENDING horizon must not enter the denominator"
    assert first.reached == 1
    assert review.pending_horizons_excluded == 1
    assert review.detections_total == 2  # both detections are still counted


# ── Idempotence ───────────────────────────────────────────────────────────────


def test_regenerating_a_period_overwrites_the_same_document(
    service, seed, firestore_client
) -> None:
    """A period has one answer. Two runs must not leave two documents."""
    from aureon.storage import paths

    seed(
        detection_list=[detection(ident="d1")],
        evaluation_list=[complete_evaluation("d1", reached=ALL_REACHED)],
    )
    moment = utc_now()
    first = service.generate_daily(MARKET_DATE, generated_at=moment)
    second = service.generate_daily(MARKET_DATE, generated_at=moment)

    docs = list(firestore_client.collection(paths.DAILY_REVIEWS).stream())
    assert len(docs) == 1
    assert first.model_dump_json() == second.model_dump_json(), (
        "a review of the same period, generated twice, is not byte-identical"
    )


def test_a_late_detection_changes_the_review_it_belongs_to(service, seed) -> None:
    """Regeneration is the recovery mechanism, so it must actually pick up new data."""
    seed(
        detection_list=[detection(ident="d1")],
        evaluation_list=[complete_evaluation("d1", reached=ALL_REACHED)],
    )
    assert service.generate_daily(MARKET_DATE).detections_total == 1

    seed(detection_list=[detection(ident="d2", minutes=120)])
    assert service.generate_daily(MARKET_DATE).detections_total == 2


# ── The market clock ──────────────────────────────────────────────────────────


def test_a_detection_before_broker_midnight_counts_towards_the_next_day(
    service, seed
) -> None:
    """22:00 UTC on 2026-09-15 is already 2026-09-16 in Athens (§63).

    A UTC-bounded query would file it under the 15th, and every per-day figure would be
    wrong in a way nothing in the output reveals.
    """
    # BASE is 10:00 UTC on the 16th; -12h is 22:00 UTC on the 15th.
    late = detection(ident="late", minutes=-12 * 60)
    assert late.detected_at.utc.day == 15
    assert late.detected_at.market_date == MARKET_DATE

    seed(detection_list=[late])
    assert service.generate_daily(MARKET_DATE).detections_total == 1
    assert service.generate_daily("2026-09-15").detections_total == 0
    # The boundary itself: broker midnight on the 16th is 21:00 UTC on the 15th. The
    # window is half-open, so that instant belongs to the 16th and the minute before it
    # belongs to the 15th. Consecutive days must tile without double-counting.
    boundary = detection(ident="boundary", minutes=-13 * 60)
    before = detection(ident="before", minutes=-13 * 60 - 1)
    assert boundary.detected_at.utc.hour == 21
    assert boundary.detected_at.market_date == MARKET_DATE
    assert before.detected_at.market_date == "2026-09-15"

    seed(detection_list=[boundary, before])
    assert service.generate_daily(MARKET_DATE).detections_total == 2
    assert service.generate_daily("2026-09-15").detections_total == 1


def test_a_weekly_review_references_only_dailies_that_exist(service, seed) -> None:
    seed(
        detection_list=[detection(ident="d1")],
        evaluation_list=[complete_evaluation("d1", reached=ALL_REACHED)],
    )
    lone = service.generate_weekly(*ISO)
    assert lone.daily_review_ids == ()

    service.generate_daily(MARKET_DATE)
    with_one = service.generate_weekly(*ISO)
    assert with_one.daily_review_ids == (MARKET_DATE,)

    weekly, dailies = service.generate_week_with_dailies(*ISO)
    assert len(dailies) == 7
    assert len(weekly.daily_review_ids) == 7
    assert weekly.detections_total == 1


# ── The Phase 7 gate: /status on a closed market ───────────────────────────────


def closed_market_state() -> SystemState:
    return SystemState(
        symbols=(
            SymbolState(
                symbol=SYMBOL, timeframe=Timeframe.M5, market_state=MarketState.CLOSED
            ),
        )
    )


def build_screen(context, settings: ExecutionSettings):
    """Exactly what ``/status`` assembles, minus the Discord transport."""

    async def run():
        state = await context.run(context.system_state.read)
        latest = await context.run(context.reviews.latest_weekly)
        if latest is None:
            latest = await context.run(context.reviews.latest_daily)
        return build_status(
            system_state=state,
            heartbeats=await context.run(context.heartbeats.read_all),
            settings=settings,
            latest_review=latest,
        )

    return asyncio.run(run())


@pytest.fixture
def context(firestore_client):
    config = AureonConfig.from_env()
    return build_context(config, firestore_client)


def test_status_on_a_closed_market_renders_the_weekly_review(
    service, seed, context, firestore_client
) -> None:
    """The Phase 7 gate.

    The weekly review is generated, stored, and then read back by the same read-only path
    ``/status`` uses -- so this fails if the review is not written, not found, or not
    renderable.
    """
    from aureon.storage.system_state_repository import SystemStateRepository

    seed(
        detection_list=[detection(ident="d1"), detection(ident="d2", minutes=45)],
        evaluation_list=[
            complete_evaluation("d1", reached=ALL_REACHED),
            DetectionEvaluation(
                detection_id="d2",
                rule_id=RULE.rule_id,
                reference_price=RULE.reference_price,
                horizons=(HorizonResult(horizon_id=HORIZON_5_CANDLES),),
            ),
        ],
        trade_list=[trade(ident="t1", minutes=5, pnl=-42.5)],
    )
    SystemStateRepository(firestore_client).write(closed_market_state())
    weekly = service.generate_weekly(*ISO)

    screen = build_screen(context, ExecutionSettings())

    assert screen.market_closed is True
    assert screen.review_summary is not None
    assert screen.review_summary != NO_REVIEW_YET
    assert screen.review_summary == summarise_review(weekly)
    # The figures a reader must be able to see, including the honesty field.
    assert "2 detections" in screen.review_summary
    assert "1 trades" in screen.review_summary
    assert "-42.50" in screen.review_summary
    assert "1 horizons still pending, excluded" in screen.review_summary
    assert RULE.rule_id in screen.review_summary


def test_status_says_so_plainly_when_no_review_exists_yet(
    context, firestore_client
) -> None:
    """An empty panel would be indistinguishable from a review of an empty week."""
    from aureon.storage.system_state_repository import SystemStateRepository

    SystemStateRepository(firestore_client).write(closed_market_state())
    screen = build_screen(context, ExecutionSettings())
    assert screen.market_closed is True
    assert screen.review_summary == NO_REVIEW_YET


def test_status_prefers_the_weekly_review_over_the_daily(
    service, seed, context, firestore_client
) -> None:
    """On a weekend the latest daily covers Friday alone; the weekly is the wider picture."""
    from aureon.storage.system_state_repository import SystemStateRepository

    seed(
        detection_list=[detection(ident="d1"), detection(ident="d2", minutes=45)],
        evaluation_list=[complete_evaluation("d1", reached=ALL_REACHED)],
    )
    SystemStateRepository(firestore_client).write(closed_market_state())
    daily = service.generate_daily(MARKET_DATE)
    weekly = service.generate_weekly(*ISO)
    assert daily.detections_total == weekly.detections_total

    screen = build_screen(context, ExecutionSettings())
    assert screen.review_summary == summarise_review(weekly)
    # Distinguishable from the daily only if the weekly is the one chosen.
    assert weekly.daily_review_ids == (MARKET_DATE,)


def test_the_status_path_holds_a_reader_that_cannot_write(context) -> None:
    """The capability split is the point, not a naming convention (§61).

    ``/status`` must be unable to overwrite a review even by mistake, so the object it
    holds has no write method at all.
    """
    assert isinstance(context.reviews, ReviewReader)
    assert not hasattr(context.reviews, "upsert_daily")
    assert not hasattr(context.reviews, "upsert_weekly")


def test_status_shows_the_most_recent_week_not_merely_a_week(
    service, seed, context, firestore_client
) -> None:
    """With several stored reviews, ``/status`` must pick the latest.

    Weekly ids sort chronologically as strings (decision 26), so "latest" is the maximum
    id. Tested with more than one review present, because with only one stored every
    selection rule -- first, last, arbitrary -- looks correct.
    """
    from aureon.storage.system_state_repository import SystemStateRepository

    # One detection in week 38, two in week 39, so the two weeks are distinguishable by
    # their counts alone.
    seed(
        detection_list=[
            detection(ident="w38"),
            detection(ident="w39a", minutes=7 * 24 * 60),
            detection(ident="w39b", minutes=7 * 24 * 60 + 30),
        ]
    )
    SystemStateRepository(firestore_client).write(closed_market_state())
    earlier = service.generate_weekly(2026, 38)
    later = service.generate_weekly(2026, 39)
    assert (earlier.detections_total, later.detections_total) == (1, 2)

    screen = build_screen(context, ExecutionSettings())
    assert screen.review_summary == summarise_review(later)
    assert "2 detections" in screen.review_summary


def test_the_latest_daily_is_the_latest_by_broker_date(service, seed) -> None:
    """The same selection rule for dailies, whose ids are broker dates."""
    seed(
        detection_list=[
            detection(ident="d1"),
            detection(ident="d2", minutes=24 * 60),
        ]
    )
    service.generate_daily(MARKET_DATE)
    service.generate_daily("2026-09-17")

    latest = ReviewReader(service._client).latest_daily()
    assert latest is not None
    assert latest.market_date == "2026-09-17"


# ── The CLI (§61-§63, decision 88) ────────────────────────────────────────────


def test_the_review_cli_generates_and_prints_both_periods(service, seed, capsys) -> None:
    """``main_review.py``'s own logic, with the service injected.

    ``build_service`` needs ``firebase-admin`` and is not exercised here. What is exercised
    is everything after it: the period defaulting, the generation, and the printing. Worth a
    test because a review is only useful if the command that produces it also reports what
    it produced -- and a format string over an empty period is exactly where that breaks.
    """
    import main_review

    seed(
        detection_list=[detection(ident="d1")],
        evaluation_list=[complete_evaluation("d1", reached=ALL_REACHED)],
        trade_list=[trade(ident="t1", minutes=5, pnl=75.5)],
    )
    now = BASE.replace(hour=23)

    assert main_review.run_daily(service, MARKET_DATE, now=now) == 0
    daily_out = capsys.readouterr().out
    assert f"daily {MARKET_DATE}" in daily_out
    assert "1 detections" in daily_out
    assert "+75.50" in daily_out
    assert "execution vs observation" in daily_out

    assert main_review.run_weekly(service, ISO, now=now, with_dailies=False) == 0
    weekly_out = capsys.readouterr().out
    assert "weekly 2026-W38" in weekly_out
    assert HORIZON_5_CANDLES in weekly_out
    assert "pending horizons excluded" in weekly_out


def test_the_cli_defaults_to_the_most_recently_completed_period(service) -> None:
    """A review of a period still accumulating is indistinguishable from a final one."""
    import main_review
    from aureon.reviews.periods import previous_iso_week, previous_market_date

    # Saturday 2026-09-19: the cron §63 asks for, firing after Friday's close. The week
    # that just traded is week 38, not week 37 — see the unit test that pins every weekday.
    now = BASE.replace(day=19, hour=12)
    assert previous_market_date(TZ, now=now) == "2026-09-18"
    assert previous_iso_week(TZ, now=now) == ISO

    assert main_review.run_weekly(service, None, now=now, with_dailies=False) == 0
    stored = ReviewReader(service._client).get_weekly(*ISO)
    assert stored is not None
    assert (stored.iso_year, stored.iso_week) == ISO


# ── The live /status snapshot (§59, §66) ──────────────────────────────────────


def open_market_state():
    """A SystemState with a fully-populated symbol, as the observer would write it."""
    from aureon.models.enums import SessionName

    return SystemState(
        symbols=(
            SymbolState(
                symbol=SYMBOL,
                timeframe=Timeframe.M5,
                market_state=MarketState.OPEN,
                ema_fast=2401.5,
                ema_slow=2399.25,
                rsi=71.4,
                session=SessionName.LONDON,
                session_trend="up",
                session_high=2405.0,
                session_low=2395.0,
                ema_crosses_today=3,
                ema_crosses_session=2,
                bullish_crosses_today=2,
                bearish_crosses_today=1,
                bullish_crosses_session=1,
                bearish_crosses_session=1,
                last_cross={
                    "direction": "buy",
                    "at": "2026-09-16T10:05:00+00:00",
                    "price": 2400.0,
                    "detection_id": "d1",
                },
                last_sweep={
                    "direction": "up",
                    "level_type": "swing_high",
                    "at": "2026-09-16T10:00:00+00:00",
                },
                last_wick={
                    "classification": "lower_rejection",
                    "at": "2026-09-16T09:50:00+00:00",
                },
                last_breakout={
                    "direction": "down",
                    "level_type": "swing_low",
                    "at": "2026-09-16T09:55:00+00:00",
                },
                detections_today=9,
            ),
        )
    )


def test_status_on_an_open_market_renders_the_full_snapshot(
    context, firestore_client
) -> None:
    """The §59 gate, through a real Firestore round trip.

    The unit test proves the layout. This proves the snapshot SURVIVES storage: the
    last-event dicts, the enum-valued session and the tz-aware timestamps all have to come
    back intact, and a field that JSON flattened into a string would render as a dash
    while looking like missing data rather than a serialisation bug.
    """
    from aureon.storage.system_state_repository import SystemStateRepository

    SystemStateRepository(firestore_client).write(open_market_state())
    screen = build_screen(context, ExecutionSettings())

    assert screen.market_closed is False
    assert screen.review_summary is None, "a live screen must not show a stale review"
    assert len(screen.live_panels) == 1

    rendered = "\n".join(screen.live_panels[0].lines)
    for expected in (
        "2401.50",
        "+2.25",
        "overbought",
        "london",
        "2405.00",
        "crosses today 3 (2↑ 1↓)",
        "last cross buy at 10:05:00Z",
        "last sweep up swing_high",
        "last breakout down swing_low",
        "last wick lower_rejection",
        "detections today 9",
    ):
        assert expected in rendered, f"{expected!r} did not survive the round trip:\n{rendered}"


def test_the_derived_fields_survive_the_round_trip(firestore_client) -> None:
    """ema_distance and rsi_zone are stored, so a reader needs no re-derivation."""
    from aureon.storage.system_state_repository import SystemStateRepository

    repository = SystemStateRepository(firestore_client)
    repository.write(open_market_state())
    read_back = repository.read()

    assert read_back is not None
    symbol_state = read_back.symbols[0]
    assert symbol_state.ema_distance == pytest.approx(2.25)
    assert symbol_state.rsi_zone == "overbought"


# ── 9A: one review per symbol, of identical shape ──────────────────────────────

SILVER = "XAGUSD"


@pytest.fixture
def silver_service(firestore_client) -> ReviewService:
    """Silver's own service: its own rule, its own documents (decision 141)."""
    from aureon.evaluation.rules import get_rule

    return ReviewService(
        firestore_client,
        get_rule("XAG_OUTCOME_V1"),
        market_tz=TZ,
        infer_window_minutes=30,
        symbol=SILVER,
    )


@pytest.fixture
def gold_service(firestore_client) -> ReviewService:
    return ReviewService(
        firestore_client, RULE, market_tz=TZ, infer_window_minutes=30, symbol=SYMBOL
    )


def test_each_symbol_gets_its_own_stored_review(
    gold_service, silver_service, seed, firestore_client
) -> None:
    """Two documents, two rule ids, no shared counts.

    One combined review would state a single ``evaluation_rule_id`` over numbers produced by
    two frozen rules, and add horizon counts measured against thresholds in different
    instruments' money.
    """
    seed(
        detection_list=[
            detection(ident="g1"),
            detection(ident="g2", minutes=10),
            detection(ident="s1", symbol=SILVER, minutes=20),
        ],
        trade_list=[trade(ident="tg", minutes=5, pnl=10.0)],
    )

    gold = gold_service.generate_daily(MARKET_DATE)
    silver = silver_service.generate_daily(MARKET_DATE)

    assert (gold.symbol, silver.symbol) == (SYMBOL, SILVER)
    assert gold.detections_total == 2
    assert silver.detections_total == 1
    assert gold.trades_total == 1
    assert silver.trades_total == 0
    assert gold.evaluation_rule_id == RULE.rule_id
    assert silver.evaluation_rule_id == "XAG_OUTCOME_V1"

    # Both are stored, and neither overwrote the other.
    reader = ReviewReader(firestore_client)
    assert reader.get_daily(MARKET_DATE, SYMBOL).detections_total == 2
    assert reader.get_daily(MARKET_DATE, SILVER).detections_total == 1


def test_the_latest_review_for_a_symbol_is_that_symbols(
    gold_service, silver_service, seed, firestore_client
) -> None:
    """Narrowed by the stored field, not by an id convention."""
    seed(
        detection_list=[detection(ident="g1"), detection(ident="s1", symbol=SILVER, minutes=5)]
    )
    gold_service.generate_weekly(*ISO)
    silver_service.generate_weekly(*ISO)

    reader = ReviewReader(firestore_client)
    assert reader.latest_weekly(SYMBOL).symbol == SYMBOL
    assert reader.latest_weekly(SILVER).symbol == SILVER
    assert reader.latest_for(SILVER).symbol == SILVER
    # An unreviewed symbol has no review, rather than somebody else's.
    assert reader.latest_weekly("EURUSD") is None
    assert reader.latest_for("EURUSD") is None


def test_regenerating_one_symbols_review_overwrites_only_its_own(
    gold_service, silver_service, seed, firestore_client
) -> None:
    seed(detection_list=[detection(ident="g1"), detection(ident="s1", symbol=SILVER)])
    gold_service.generate_daily(MARKET_DATE)
    silver_service.generate_daily(MARKET_DATE)

    seed(detection_list=[detection(ident="g2", minutes=30)])
    regenerated = gold_service.generate_daily(MARKET_DATE)

    reader = ReviewReader(firestore_client)
    assert regenerated.detections_total == 2
    assert reader.get_daily(MARKET_DATE, SYMBOL).detections_total == 2
    assert reader.get_daily(MARKET_DATE, SILVER).detections_total == 1


def test_a_scoped_status_shows_that_symbols_review_on_a_closed_market(
    context, gold_service, silver_service, seed, firestore_client
) -> None:
    """9A's done-when for the weekly mode, per symbol."""
    import asyncio

    from aureon.discord.commands.status import StatusCommands
    from aureon.models.system import SymbolState, SystemState
    from aureon.storage.system_state_repository import SystemStateRepository

    seed(
        detection_list=[
            detection(ident="g1"),
            detection(ident="s1", symbol=SILVER, minutes=5),
            detection(ident="s2", symbol=SILVER, minutes=10),
        ]
    )
    gold_service.generate_weekly(*ISO)
    silver_service.generate_weekly(*ISO)

    SystemStateRepository(firestore_client).write(
        SystemState(
            symbols=(
                SymbolState(
                    symbol=SILVER, timeframe=Timeframe.M5, market_state=MarketState.CLOSED
                ),
            ),
            updated_at=utc_now(),
        ),
        force=True,
    )

    screen = asyncio.run(StatusCommands(context)._build(SILVER))  # noqa: SLF001
    assert screen.market_closed is True
    assert screen.review_summary is not None
    assert "2 detections" in screen.review_summary  # silver's count, not gold's one
    assert "XAG_OUTCOME_V1" in screen.review_summary

"""``/monitor`` end to end, against the emulator (9D, §62-§64).

``tests/unit/test_assessment_service.py`` pins the arithmetic. What only this can show is
the property the command exists to preserve and is most likely to lose quietly: that a
readout a human reads is **the readout that was stored**, built from rows that were already
in Firestore, with the trend read the observer published rather than one Discord invented.

Three failures are worth more than the rest, because each produces a screen that looks
entirely reasonable:

* an assessment rendered from a cohort that had not met the floor — a percentage from eleven
  detections reads exactly like one from three hundred;
* a trend read Discord made up because the observer had not published one;
* a target or stop that reaches an order. Nothing may prefill either, ever (§64).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from aureon.config import AureonConfig
from aureon.discord.commands.monitor import MonitorCommands
from aureon.discord.context import build_context
from aureon.evaluation.rules import XAU_OUTCOME_V2
from aureon.models.assessment import MIN_COHORT
from aureon.models.base import MarketTime, utc_now
from aureon.models.detection import Detection, IndicatorSnapshot, SessionContext
from aureon.models.enums import (
    Direction,
    HorizonKind,
    HorizonStatus,
    MarketState,
    PathClassification,
    SessionName,
    Timeframe,
    TrendBias,
)
from aureon.models.evaluation import DetectionEvaluation, HorizonResult, threshold_key
from aureon.models.profile import VolatilityContext, VolumeProfileRef
from aureon.models.system import SymbolState, SystemState
from aureon.storage.detection_repository import DetectionRepository
from aureon.storage.evaluation_repository import EvaluationRepository
from aureon.storage.system_state_repository import SystemStateRepository
from tests.failure_injection.conftest import USER, FakeInteraction, embed_text

pytestmark = pytest.mark.emulator

GOLD = "XAUUSD"
TZ = "Europe/Athens"
HORIZON = XAU_OUTCOME_V2.horizons[2].id
POINT = 0.01


@pytest.fixture
def context(firestore_client):
    return build_context(
        AureonConfig(
            symbols=(GOLD,),
            evaluation_rules={GOLD: "XAU_OUTCOME_V2"},
            authorized_user_ids=(USER,),
        ),
        firestore_client,
    )


def detection(ident: str, *, minutes_ago: float, direction=Direction.BUY) -> Detection:
    moment = utc_now() - timedelta(minutes=minutes_ago)
    return Detection(
        detection_id=ident,
        account_scope="primary",
        symbol=GOLD,
        timeframe=Timeframe.M5,
        agent_name="ema_cross",
        agent_version="2.1.0",
        event_key="bullish",
        direction=direction,
        detected_at=MarketTime.from_utc(moment, TZ),
        candle_open_time=MarketTime.from_utc(moment - timedelta(minutes=5), TZ),
        price=2400.00,
        indicators=IndicatorSnapshot(ema={"fast": 2401.0, "slow": 2395.0}, rsi=61.4),
        session=SessionContext(session=SessionName.LONDON, session_config_version=1),
        sequence_today=1,
        sequence_session=1,
        volume_profile_ref=VolumeProfileRef(scope="asia", price_vs_va="inside"),
        volatility=VolatilityContext(regime="normal", bands_version=1),
    )


#: Every COUNTED horizon, as a real evaluation carries. A fixture populating one horizon
#: made the estimates read empty while the cohort read 35 -- the estimate horizon defaults
#: to the last counted one, and the fixture had filled a different column.
COUNTED_HORIZONS = tuple(
    h.id for h in XAU_OUTCOME_V2.horizons if h.kind in {HorizonKind.CANDLES, HorizonKind.MINUTES}
)


def evaluation(ident: str, *, mfe: float = 400.0, mae: float = 150.0) -> DetectionEvaluation:
    reached = {threshold_key(t): t <= 5.0 for t in XAU_OUTCOME_V2.thresholds}
    return DetectionEvaluation(
        detection_id=ident,
        rule_id=XAU_OUTCOME_V2.rule_id,
        reference_price=XAU_OUTCOME_V2.reference_price,
        reference_value=2400.0,
        horizons=tuple(
            HorizonResult(
                horizon_id=horizon_id,
                status=HorizonStatus.COMPLETE,
                future_high=2410.0,
                future_low=2395.0,
                mfe=mfe,
                mae=mae,
                reached=reached,
                time_to={k: (600.0 if v else None) for k, v in reached.items()},
                path=PathClassification.MFE_FIRST,
                candles_seen=20,
                completed_at=utc_now(),
            )
            for horizon_id in COUNTED_HORIZONS
        ),
        context_tags={"session_trend_aligned": True},
    )


@pytest.fixture
def history(firestore_client):
    """``count`` prior detections, each already evaluated, all before the subject."""

    def store(count: int) -> None:
        detections = DetectionRepository(firestore_client)
        evaluations = EvaluationRepository(firestore_client)
        for i in range(count):
            ident = f"hist-{i:03d}"
            detections.upsert(detection(ident, minutes_ago=600 + i))
            evaluations.upsert(evaluation(ident))

    return store


@pytest.fixture
def subject(firestore_client):
    def store(*, minutes_ago: float = 5.0, ident: str = "subject") -> Detection:
        found = detection(ident, minutes_ago=minutes_ago)
        DetectionRepository(firestore_client).upsert(found)
        return found

    return store


@pytest.fixture
def trend_published(firestore_client):
    """What the OBSERVER writes: a trend read inside the symbol's state document."""

    def publish(bias: TrendBias = TrendBias.BULLISH) -> None:
        from aureon.models.assessment import TrendRead

        SystemStateRepository(firestore_client).write(
            SystemState(
                symbols=(
                    SymbolState(
                        symbol=GOLD,
                        timeframe=Timeframe.M5,
                        market_state=MarketState.OPEN,
                        trend_read=TrendRead(
                            bias=bias,
                            evidence=("ema fast 2401.00 above slow 2395.00",),
                            candles=60,
                            as_of=utc_now(),
                        ),
                    ),
                )
            ),
            force=True,
        )

    return publish


def run_monitor(context, interaction, *, symbol=GOLD, detection_id=None):
    asyncio.run(MonitorCommands(context).monitor(interaction, symbol, detection_id))


def stored_assessments(firestore_client) -> list[dict]:
    from aureon.storage import paths

    return [
        doc.to_dict() for doc in firestore_client.collection(paths.ASSESSMENTS).stream()
    ]


# ── The done-when ─────────────────────────────────────────────────────────────


def test_a_bullish_london_cross_gets_bias_n_confirmation_and_quantiles(
    context, firestore_client, history, subject, trend_published
) -> None:
    """9D's Done-when, in one call.

    Bias, a cohort of at least thirty, per-horizon confirmation with intervals, TP and SL
    quantiles, and the assessment stored where the weekly review can find it.
    """
    history(MIN_COHORT + 5)
    found = subject()
    trend_published()

    interaction = FakeInteraction()
    run_monitor(context, interaction)

    text = embed_text(interaction.embeds[0])
    assert "bullish" in text
    assert f"n={MIN_COHORT + 5}" in text
    assert "CI" in text, "every percentage carries its interval"
    assert "Target, measured" in text and "Adverse, measured" in text
    assert "not advice" in text

    stored = stored_assessments(firestore_client)
    assert len(stored) == 1
    assert stored[0]["detection_id"] == found.detection_id
    assert stored[0]["rule_id"] == "XAU_OUTCOME_V2"
    assert stored[0]["n"] == MIN_COHORT + 5
    assert stored[0]["insufficient"] is False


def test_too_little_history_says_so_and_publishes_no_rate(
    context, firestore_client, history, subject, trend_published
) -> None:
    """The refusal is the feature. A rate from eleven prior detections is read exactly like
    a rate from three hundred."""
    history(MIN_COHORT - 1)
    subject()
    trend_published()

    interaction = FakeInteraction()
    run_monitor(context, interaction)

    text = embed_text(interaction.embeds[0])
    assert f"insufficient history (n={MIN_COHORT - 1}" in text
    assert "%" not in text, "no percentage at all, not a smaller one"
    assert "Target, measured" not in text

    # Still stored: "we looked and there was not enough history" is a finding worth counting.
    stored = stored_assessments(firestore_client)
    assert len(stored) == 1 and stored[0]["insufficient"] is True
    assert stored[0]["tp_estimates"] == [] and stored[0]["sl_estimates"] == []


def test_without_a_published_trend_read_nothing_is_invented(
    context, firestore_client, history, subject
) -> None:
    """Discord holds no data provider, so the only honest answer is to say the read is
    missing — a fabricated bias would be indistinguishable from a measured one."""
    history(MIN_COHORT)
    subject()

    interaction = FakeInteraction()
    run_monitor(context, interaction)

    text = embed_text(interaction.embeds[0])
    assert "no trend read published" in text
    assert "sideways" in text

    stored = stored_assessments(firestore_client)
    assert stored[0]["trend_read"]["candles"] == 0


def test_the_published_trend_read_is_what_is_shown(
    context, firestore_client, history, subject, trend_published
) -> None:
    """Read from Firestore, not recomputed: the observer and Discord cannot come to
    different conclusions about the same window."""
    history(MIN_COHORT)
    subject()
    trend_published(TrendBias.BEARISH)

    interaction = FakeInteraction()
    run_monitor(context, interaction)

    text = embed_text(interaction.embeds[0])
    assert "bearish" in text
    assert "ema fast 2401.00 above slow 2395.00" in text


def test_a_trend_read_against_the_detection_is_said_out_loud(
    context, firestore_client, history, subject, trend_published
) -> None:
    history(MIN_COHORT)
    subject()
    trend_published(TrendBias.BEARISH)  # the subject is a BUY

    interaction = FakeInteraction()
    run_monitor(context, interaction)

    text = embed_text(interaction.embeds[0])
    assert "disagree" in text
    assert stored_assessments(firestore_client)[0]["disagrees_with_detection"] is True


# ── What it refuses ───────────────────────────────────────────────────────────


def test_nothing_it_shows_ever_reaches_an_order(
    context, firestore_client, history, subject, trend_published
) -> None:
    """§64, stated as a count. A readout writes ONE kind of document and it is not a trade."""
    from aureon.storage import paths

    history(MIN_COHORT)
    subject()
    trend_published()
    run_monitor(context, FakeInteraction())

    for collection in (paths.TRADE_REQUESTS, paths.TRADES, paths.CONTROL_REQUESTS):
        assert list(firestore_client.collection(collection).stream()) == [], collection


def test_a_detection_from_another_symbol_is_refused(
    context, firestore_client, history, subject, trend_published
) -> None:
    """A mistyped id is a mistake, not a request to switch instruments (9A-6's cross-check)."""
    history(MIN_COHORT)
    subject()
    trend_published()

    silver = detection("silver-1", minutes_ago=5).model_copy(update={"symbol": "XAGUSD"})
    DetectionRepository(firestore_client).upsert(silver)

    interaction = FakeInteraction()
    run_monitor(context, interaction, detection_id="silver-1")
    assert "No detection" in embed_text(interaction.embeds[0])
    assert stored_assessments(firestore_client) == []


def test_an_unobserved_symbol_is_refused(context, firestore_client) -> None:
    interaction = FakeInteraction()
    run_monitor(context, interaction, symbol="XAGUSD")
    assert "No such symbol here" in embed_text(interaction.embeds[0])


def test_nothing_recent_says_so_rather_than_reaching_further_back(
    context, firestore_client, history, subject, trend_published
) -> None:
    """Answering with a cross from three hours ago would answer a different question
    without saying so."""
    history(MIN_COHORT)
    subject(minutes_ago=600, ident="old-one")
    trend_published()

    interaction = FakeInteraction()
    run_monitor(context, interaction)
    assert "No recent detection" in embed_text(interaction.embeds[0])
    assert stored_assessments(firestore_client) == []

    # But naming it explicitly works: the window is about the default, not about permission.
    named = FakeInteraction()
    run_monitor(context, named, detection_id="old-one")
    assert "assessment" in embed_text(named.embeds[0])


def test_the_cohort_excludes_detections_made_after_the_subject(
    context, firestore_client, history, subject, trend_published
) -> None:
    """Look-ahead, at the level where it would actually be introduced: a query over the
    collection, with nothing about the rows saying when they happened.

    Two guards stop it -- the read window ends at the subject's own timestamp, and
    ``population_from`` drops anything at or after it -- so breaking either one alone still
    gives the right answer here, and only breaking both turns this red. That is deliberate
    defence in depth rather than an accident: the read window is an optimisation that a
    later change might widen for perfectly good reasons, and the rule has to survive that.
    ``tests/unit/test_assessment_service.py`` pins the service guard on its own.
    """
    history(MIN_COHORT)
    found = subject(minutes_ago=300, ident="middle")

    detections = DetectionRepository(firestore_client)
    evaluations = EvaluationRepository(firestore_client)
    for i in range(20):
        later = f"later-{i}"
        detections.upsert(detection(later, minutes_ago=10 + i))
        evaluations.upsert(evaluation(later))

    trend_published()
    run_monitor(context, FakeInteraction(), detection_id=found.detection_id)

    stored = stored_assessments(firestore_client)[0]
    assert stored["n"] == MIN_COHORT, "the twenty later detections were counted"


def test_an_unauthorized_reader_gets_nothing(context, firestore_client) -> None:
    interaction = FakeInteraction("user-999")
    run_monitor(context, interaction)
    assert "Not authorized" in embed_text(interaction.embeds[0])
    assert stored_assessments(firestore_client) == []


# ── The other half: the observer publishing the trend read ───────────────────


@pytest.fixture
def observer_with_state(tmp_path, firestore_client, candles):
    """A real Observer over the fixture week, writing real state documents (9D).

    Needed because everything above publishes the trend read by hand. Without this, an
    observer that silently published ``None`` forever would leave every test on this page
    green and every real `/monitor` screen reading "no trend read published" — the two
    halves have to meet somewhere.
    """
    from aureon.config import AureonConfig
    from aureon.outbox.local_outbox import LocalOutbox
    from aureon.outbox.outbox_worker import OutboxWorker
    from aureon.services.observer_state import ObserverState
    from main_observer import Observer
    from tests.conftest import FakeLiveProvider, cross_agent

    config = AureonConfig.from_env(
        env={
            "AUREON_ACCOUNT_SCOPE": "primary",
            "AUREON_MARKET_TZ": "Europe/Athens",
            "AUREON_SYMBOLS": GOLD,
            "AUREON_TIMEFRAMES": "M5",
            "AUREON_OUTBOX_PATH": str(tmp_path / "outbox.db"),
            "AUREON_OBSERVER_STATE_PATH": str(tmp_path / "observer_state.json"),
        }
    )
    outbox = LocalOutbox(config.outbox_path)
    observer = Observer(
        config,
        FakeLiveProvider(candles),
        outbox=outbox,
        worker=OutboxWorker(outbox, DetectionRepository(firestore_client).upsert_payload),
        state=ObserverState(config.observer_state_path),
        agents=[cross_agent()],
        state_repository=SystemStateRepository(firestore_client),
    )
    yield observer, outbox
    outbox.close()


def test_the_observer_publishes_a_trend_read_a_human_could_check(
    observer_with_state, firestore_client, candles
) -> None:
    """Real candles in, a real state document out, with the working shown.

    Asserts the evidence rather than only the bias: a bias is one of three words and would
    be produced by any stub, where "swings: 3 higher highs, …" can only come from candles
    that were actually read.
    """
    observer, _ = observer_with_state
    provider = observer.provider

    provider.set_clock(candles[0].open_time.utc)
    for candle in candles[:200]:
        provider.advance_to_close_of(candle)
        observer.market_engine.poll_once()
    observer._write_system_state(force=True)

    state = SystemStateRepository(firestore_client).read_symbol(GOLD, Timeframe.M5)
    published = state.symbols[0].trend_read
    assert published is not None, "the observer published no trend read at all"
    assert published.candles > 0
    assert published.bias in set(TrendBias)

    joined = " | ".join(published.evidence)
    assert "swings:" in joined, "the structure count is missing"
    assert "ema" in joined
    # The slope is only computable once the window is longer than the fast period; two
    # hundred candles is well past it, so "unknown" here would mean the EMA never arrived.
    assert "ema slope unknown" not in joined


def test_monitor_renders_the_read_the_observer_published(
    observer_with_state, firestore_client, context, history, subject, candles
) -> None:
    """The join. Neither half is worth anything without the other."""
    observer, _ = observer_with_state
    provider = observer.provider
    provider.set_clock(candles[0].open_time.utc)
    for candle in candles[:200]:
        provider.advance_to_close_of(candle)
        observer.market_engine.poll_once()
    observer._write_system_state(force=True)

    state = SystemStateRepository(firestore_client).read_symbol(GOLD, Timeframe.M5)
    published = state.symbols[0].trend_read

    history(MIN_COHORT)
    subject()
    interaction = FakeInteraction()
    run_monitor(context, interaction)

    text = embed_text(interaction.embeds[0])
    assert published.bias.value in text
    assert published.evidence[0] in text
    assert "no trend read published" not in text

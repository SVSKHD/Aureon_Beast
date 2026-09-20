"""Announcing detections and fired reminders (9C).

The property under test is the one a channel makes obvious when it breaks: **each thing is
said once**. A bot restart, a slow write, a sweep that overlaps the previous one — none may
produce a second embed for the same detection.

The send is injected rather than mocked out of a real client, so these run without a gateway
and assert on what would have been posted. Every field in a detection embed comes from the
stored detection and every field in a reminder from the frozen snapshot: this module decides
*whether* to post, never what is true.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta

import pytest

from aureon.config import AureonConfig
from aureon.discord.context import BotContext
from aureon.discord.notifier import Notifier
from aureon.discord.service import RESEARCH_ONLY, build_notification
from aureon.models.alerts import PriceAlert
from aureon.models.base import MarketTime
from aureon.models.detection import Detection, IndicatorSnapshot, SessionContext
from aureon.models.enums import (
    Direction,
    NotificationKind,
    PriceAlertStatus,
    SessionName,
    Timeframe,
)
from aureon.models.profile import VolatilityContext, VolumeProfileRef
from aureon.models.settings import NotificationSettings
from aureon.storage.alert_repository import PriceAlertRepository
from aureon.storage.detection_repository import DetectionRepository
from aureon.storage.notification_repository import NotificationRepository
from aureon.storage.settings_repository import (
    ExecutionSettingsRepository,
    NotificationSettingsRepository,
)
from aureon.storage.symbol_repository import SymbolRepository
from aureon.storage.system_state_repository import (
    HeartbeatRepository,
    SystemStateRepository,
)
from aureon.storage.trade_repository import TradeRepository
from aureon.storage.trade_request_repository import TradeRequestRepository

TZ = "Europe/Athens"
NOW = datetime(2026, 9, 16, 10, 0, tzinfo=UTC)
SYMBOL = "XAUUSD"
CHANNEL = 4242
USER = "user-1"


class Recorder:
    """The injected send: records what would have gone to Discord."""

    def __init__(self, *, fail: bool = False) -> None:
        self.posts: list[dict] = []
        self.fail = fail

    async def __call__(self, target, *, embed=None, view=None, direct: bool = False):
        if self.fail:
            raise RuntimeError("403 forbidden")
        self.posts.append(
            {"target": target, "embed": embed, "view": view, "direct": direct}
        )


def detection(
    *,
    ident: str = "det-1",
    agent: str = "ema_cross",
    minutes_ago: float = 0.5,
    symbol: str = SYMBOL,
    direction: Direction | None = Direction.BUY,
) -> Detection:
    moment = NOW - timedelta(minutes=minutes_ago)
    return Detection(
        detection_id=ident,
        account_scope="primary",
        symbol=symbol,
        timeframe=Timeframe.M5,
        agent_name=agent,
        agent_version="2.1.0",
        event_key="bullish",
        direction=direction,
        detected_at=MarketTime.from_utc(moment, TZ),
        candle_open_time=MarketTime.from_utc(moment - timedelta(minutes=5), TZ),
        price=2400.50,
        indicators=IndicatorSnapshot(ema={"fast": 2401.0, "slow": 2395.0}, rsi=61.4),
        session=SessionContext(session=SessionName.LONDON, session_config_version=1),
        sequence_today=1,
        sequence_session=1,
        volume_profile_ref=VolumeProfileRef(
            scope="asia",
            poc_price=2398.0,
            va_low=2396.0,
            va_high=2402.0,
            price_vs_va="inside",
            nearest_lvn=2404.0,
            nearest_hvn=2398.0,
        ),
        volatility=VolatilityContext(
            atr_14=1.25, atr_points=125.0, regime="high", session_range_vs_median=1.8,
            bands_version=1,
        ),
    )


@pytest.fixture
def context(firestore) -> BotContext:
    config = AureonConfig(
        symbols=(SYMBOL,),
        authorized_user_ids=(USER,),
        alert_channel_id=CHANNEL,
        notify_window_seconds=120.0,
    )
    return BotContext(
        config=config,
        requests=TradeRequestRepository(firestore),
        controls=None,
        trades=TradeRepository(firestore),
        detections=DetectionRepository(firestore),
        settings=ExecutionSettingsRepository(firestore),
        reviews=None,
        symbols=SymbolRepository(firestore),
        system_state=SystemStateRepository(firestore),
        heartbeats=HeartbeatRepository(firestore),
        alerts=PriceAlertRepository(firestore),
        notifications=NotificationRepository(firestore),
        notification_settings=NotificationSettingsRepository(firestore),
    )


def sweep(notifier: Notifier, *, now: datetime = NOW):
    return asyncio.run(notifier.sweep(now=now))


def notifier_for(context: BotContext, recorder: Recorder, **kwargs) -> Notifier:
    return Notifier(context, send=recorder, **kwargs)


# ── Exactly once ──────────────────────────────────────────────────────────────


def test_a_detection_is_announced_once_however_many_sweeps_run(
    context, firestore
) -> None:
    """The sweeps overlap on purpose: the window is two minutes and the poll is seconds."""
    context.detections.upsert(detection())
    recorder = Recorder()
    notifier = notifier_for(context, recorder)

    first = sweep(notifier)
    assert first.detections == ["det-1"]
    assert len(recorder.posts) == 1

    for _ in range(3):
        assert sweep(notifier).detections == []
    assert len(recorder.posts) == 1


def test_a_restarted_bot_announces_nothing_it_already_said(context, firestore) -> None:
    """The record is a document precisely so a restart cannot repeat itself."""
    context.detections.upsert(detection())
    first = Recorder()
    sweep(notifier_for(context, first))
    assert len(first.posts) == 1

    second = Recorder()
    assert sweep(notifier_for(context, second)).detections == []
    assert second.posts == []


def test_a_failed_post_is_recorded_and_not_retried(context, firestore) -> None:
    """A retry over a channel that is rejecting messages either double-posts or hides the
    outage. The row is for the operator."""
    context.detections.upsert(detection())
    failing = Recorder(fail=True)
    assert sweep(notifier_for(context, failing)).detections == []

    record = context.notifications.get(NotificationKind.DETECTION, "det-1")
    assert record is not None
    assert record.status.value == "failed"
    assert "403" in record.failure_message

    working = Recorder()
    assert sweep(notifier_for(context, working)).detections == []
    assert working.posts == []


# ── What is announced ─────────────────────────────────────────────────────────


def test_only_the_enabled_agents_are_announced(context, firestore) -> None:
    """The settings document decides, read fresh: silencing a noisy agent at 02:00 must
    take effect on the next detection rather than the next deploy."""
    context.detections.upsert(detection(ident="cross", agent="ema_cross"))
    context.detections.upsert(detection(ident="rsi-1", agent="rsi"))

    recorder = Recorder()
    assert sweep(notifier_for(context, recorder)).detections == ["cross"]

    context.notification_settings.write(
        NotificationSettings(enabled_kinds=("rsi",))
    )
    later = Recorder()
    assert sweep(notifier_for(context, later)).detections == ["rsi-1"]


def test_detections_can_be_silenced_entirely_without_forgetting_the_kinds(
    context, firestore
) -> None:
    context.detections.upsert(detection())
    context.notification_settings.write(
        NotificationSettings(detections_enabled=False)
    )
    recorder = Recorder()
    assert sweep(notifier_for(context, recorder)).detections == []
    assert context.notification_settings.read_or_default().enabled_kinds


def test_a_detection_older_than_the_window_is_not_announced(context, firestore) -> None:
    """A bot down for an hour wakes and posts nothing, rather than an hour of history: a
    channel full of stale detections is worse than a gap, because the gap is visible."""
    context.detections.upsert(detection(ident="old", minutes_ago=45))
    recorder = Recorder()
    assert sweep(notifier_for(context, recorder)).detections == []


def test_nothing_is_announced_without_a_channel(context, firestore) -> None:
    """A deployment that has not chosen a channel must not post into whichever it can see."""
    context.detections.upsert(detection())
    context.config = context.config.model_copy(update={"alert_channel_id": None})
    recorder = Recorder()
    notifier = Notifier(context, send=recorder, channel_id=None)
    assert sweep(notifier).total == 0
    assert recorder.posts == []


def test_the_embed_carries_every_line_the_phase_names(context) -> None:
    """Symbol, direction, price, EMA + relation, RSI + zone, session, volume, volatility,
    and the research-only footer (9C)."""
    screen = build_notification(detection())
    text = "\n".join(f"{name} {value}" for name, value in screen.fields)
    assert SYMBOL in screen.title and "buy" in screen.title
    assert "2400.50" in text
    assert "2401.00 / 2395.00 — fast above slow" in text
    assert "61.4 neutral" in text
    assert "london" in text
    assert "inside VA" in text and "POC 2398.00" in text
    assert "LVN 2404.00" in text and "HVN 2398.00" in text
    assert "high · ATR14 1.25" in text
    assert RESEARCH_ONLY in screen.footer
    assert screen.detection_id in screen.footer


def test_a_detection_without_context_renders_the_rows_anyway(context) -> None:
    """Two embeds of the same agent must have the same shape, or a reader cannot compare
    them at a glance."""
    bare = detection().model_copy(update={"volume_profile_ref": None, "volatility": None})
    screen = build_notification(bare)
    names = [name for name, _ in screen.fields]
    assert "Volume" in names and "Volatility" in names
    values = dict(screen.fields)
    assert "no profile yet" in values["Volume"]
    assert values["Volatility"] == "—"


def test_the_embeds_zone_is_the_agents_zone(context) -> None:
    """One definition of overbought, not two.

    The detection stores 61.4 and no label, so the embed applies one — and it has to be the
    label the RSI agent would apply. A 70/30 written out in the Discord module would
    eventually disagree with the agent's, and the embed is exactly where a human would read
    that disagreement with no way to notice it.
    """
    from aureon.agents.rsi_agent import RsiAgent
    from aureon.discord.service import _rsi_zone

    agent = RsiAgent()
    assert _rsi_zone(agent.overbought) == "overbought"
    assert _rsi_zone(agent.oversold) == "oversold"
    assert _rsi_zone((agent.overbought + agent.oversold) / 2) == "neutral"
    assert _rsi_zone(None) == "—"


def test_a_context_only_detection_offers_no_execute_button(context) -> None:
    """There is no side to prefill, and inventing one is the guess this design refuses."""
    screen = build_notification(detection(direction=None))
    assert screen.side is None
    assert "context" in screen.title


# ── Fired reminders ───────────────────────────────────────────────────────────


def fired_alert(**overrides) -> PriceAlert:
    base = dict(
        alert_id="al-1",
        symbol=SYMBOL,
        level=2450.0,
        side="above",
        requested_by=USER,
        status=PriceAlertStatus.FIRED,
        fired_at=NOW - timedelta(seconds=30),
        fired_price=2450.10,
        note="range high",
        fired_snapshot={
            "ema_fast": 2451.0,
            "ema_slow": 2440.0,
            "ema_relation": "above",
            "last_cross": {
                "direction": "buy",
                "at": (NOW - timedelta(minutes=42)).isoformat(),
            },
            "rsi": 61.4,
            "rsi_zone": "neutral",
            "session": "london",
            "session_trend": "up",
            "session_high": 2455.0,
            "session_low": 2438.0,
            "ema_distance": 11.0,
            "detections_today": 37,
            "last_sweep": {"direction": "buy", "level_type": "asia_low"},
            "last_breakout": {"direction": "buy", "level_type": "session_high"},
            "minutes_since_cross": 42.0,
            "last_wick": {"classification": "lower_rejection"},
            "volatility": {"regime": "high", "atr_14": 1.25},
            "volume_profile": {"asia": {"poc_price": 2444.0, "value_area_low": 2440.0,
                                        "value_area_high": 2448.0}},
        },
    )
    return PriceAlert(**(base | overrides))


def store(firestore, alert: PriceAlert) -> None:
    from aureon.storage import paths

    firestore.document(paths.alert_path(alert.alert_id)).set(alert.model_dump(mode="json"))


def test_a_fired_alert_is_sent_to_its_requester_not_the_channel(
    context, firestore
) -> None:
    """An alert is one person's question, and the channel is readable by more people than
    armed it."""
    store(firestore, fired_alert())
    recorder = Recorder()
    assert sweep(notifier_for(context, recorder)).reminders == ["al-1"]
    post = recorder.posts[0]
    assert post["target"] == USER
    assert post["direct"] is True


def test_a_reminder_is_rendered_from_the_frozen_snapshot(context, firestore) -> None:
    """Not one value is read live: the message describes the market that crossed the level,
    not the one a few seconds later."""
    from aureon.discord.service import build_reminder

    screen = build_reminder(fired_alert())
    text = "\n".join(f"{name} {value}" for name, value in screen.fields)
    assert "2450.10" in text and "level 2450" in text
    assert "2451.00 / 2440.00 — fast above" in text
    assert "buy, 42m ago" in text
    assert "61.4 neutral" in text
    assert "london up" in text
    assert "POC 2444.00" in text
    assert "high · ATR14 1.25" in text
    assert "lower_rejection" in text
    assert "2438.00–2455.00" in text
    assert "by 11" in text
    assert screen.note == "range high"
    assert screen.side == "buy"


#: Snapshot keys the reminder deliberately does NOT render, each with its reason. Anything
#: else ``build_snapshot`` writes has to appear, or the test below fails — which is the
#: point: a field added to the snapshot and forgotten in the embed is invisible, and the
#: embed is the only place a human ever sees the frozen market.
NOT_RENDERED = {
    # The transacted side is already the headline "Price"; the pair and its timestamp
    # would repeat it in a message whose whole subject is one price at one moment.
    "bid",
    "ask",
    "captured_at",
}


def test_every_frozen_field_reaches_the_embed(context) -> None:
    """Built by ``build_snapshot`` itself, not by a hand-written dict.

    A fixture can only forget a field the same way the renderer can. Asking the writer what
    it writes is the only version of this test that stays true when 9D adds a field.
    """
    from aureon.discord.service import build_reminder
    from aureon.services.alert_watcher import build_snapshot

    snapshot = build_snapshot(
        quote=None,
        state_fields={
            "ema_fast": 2451.0,
            "ema_slow": 2440.0,
            "ema_distance": 11.0,
            "rsi": 61.4,
            "rsi_zone": "neutral",
            "session": "london",
            "session_trend": "up",
            "session_high": 2455.0,
            "session_low": 2438.0,
            "last_cross": {"direction": "buy"},
            "last_sweep": {"direction": "buy", "level_type": "asia_low"},
            "last_wick": {"classification": "lower_rejection"},
            "last_breakout": {"direction": "buy", "level_type": "session_high"},
            "detections_today": 37,
            "last_cross_at": NOW - timedelta(minutes=42),
        },
    )
    screen = build_reminder(fired_alert(fired_snapshot=snapshot))
    rendered = " ".join(f"{name} {value}" for name, value in screen.fields)

    missing = [
        key
        for key in snapshot
        if key not in NOT_RENDERED and not _mentions(rendered, snapshot[key])
    ]
    assert missing == [], f"frozen but never shown to anyone: {missing}"


def _mentions(text: str, value: object) -> bool:
    """Is this value visible in the rendered embed?

    Matched on word boundaries rather than as a bare substring. A plain ``in`` found
    ``detections_today = 3`` inside the "2438.00" of the session range, so dropping the row
    entirely left the test green — the same vacuous-assertion shape as decisions 116 and
    188, discovered here by planting the deletion.
    """
    if value is None:
        return True  # nothing to show; the row still renders as "—"
    if isinstance(value, dict):
        # ALL of them, not any: an event's direction and its level type are two facts, and
        # "any" was satisfied by a "buy" the neighbouring row happened to render -- so
        # deleting the whole Last sweep line left the test green.
        return all(_mentions(text, inner) for inner in value.values())
    if isinstance(value, float):
        forms = {f"{value:g}", f"{value:.2f}", f"{value:.1f}"}
    else:
        forms = {str(value)}
    # Bounded by digits and dots only, not by letters: "42m ago" is a rendering of 42.
    return any(re.search(rf"(?<![\d.]){re.escape(form)}(?![\d.])", text) for form in forms)


def test_a_reminder_is_sent_once(context, firestore) -> None:
    store(firestore, fired_alert())
    recorder = Recorder()
    notifier = notifier_for(context, recorder)
    assert sweep(notifier).reminders == ["al-1"]
    assert sweep(notifier).reminders == []
    assert len(recorder.posts) == 1


def test_an_alert_that_fired_before_the_window_is_not_re_sent(context, firestore) -> None:
    store(firestore, fired_alert(fired_at=NOW - timedelta(hours=2)))
    recorder = Recorder()
    assert sweep(notifier_for(context, recorder)).reminders == []


def test_an_armed_alert_is_not_announced(context, firestore) -> None:
    store(
        firestore,
        PriceAlert(
            alert_id="al-armed",
            symbol=SYMBOL,
            level=2450.0,
            side="above",
            requested_by=USER,
        ),
    )
    recorder = Recorder()
    assert sweep(notifier_for(context, recorder)).reminders == []


def test_a_reminder_for_a_below_alert_prefills_sell(context) -> None:
    from aureon.discord.service import build_reminder

    assert build_reminder(fired_alert(side="below")).side == "sell"

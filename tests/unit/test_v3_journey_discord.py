"""Discord journey-thread and closeout behavior for Aureon V3."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

from aureon.discord.notifier import Notifier
from aureon.models.enums import Direction, NotificationKind


class FakeNotifications:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], SimpleNamespace] = {}

    def get(self, kind, ref_id):
        return self.rows.get((str(kind), ref_id))

    def claim(self, kind, ref_id, *, symbol, channel_id, now):
        key = (str(kind), ref_id)
        if key in self.rows:
            return None
        row = SimpleNamespace(
            kind=kind,
            ref_id=ref_id,
            symbol=symbol,
            channel_id=channel_id,
            message_id=None,
            failure_message=None,
        )
        self.rows[key] = row
        return row

    def record_message(self, kind, ref_id, message_id):
        row = self.rows[(str(kind), ref_id)]
        row.message_id = str(message_id)
        return row

    def mark_failed(self, kind, ref_id, *, message):
        row = self.rows[(str(kind), ref_id)]
        row.failure_message = message
        return row


class FakeV3:
    def prediction_for_detection(self, detection_id):
        return {
            "payload": {
                "sufficient_data": True,
                "sample_count": 100,
                "target_sample_counts": {"reach_3": 90, "reach_10": 80},
                "probability_reach_3": 0.82,
                "probability_reach_10": 0.61,
            }
        }


class FakeContext:
    def __init__(self) -> None:
        self.config = SimpleNamespace(
            alert_channel_id=4242,
            notify_window_seconds=120.0,
        )
        self.notifications = FakeNotifications()
        self.v3_ema = FakeV3()

    async def run(self, fn, *args, **kwargs):
        return fn(*args, **kwargs)


def _journey():
    outcome = SimpleNamespace(
        mfe=12.0,
        mae=2.0,
        targets={
            "3": SimpleNamespace(reached=True),
            "5": SimpleNamespace(reached=True),
            "10": SimpleNamespace(reached=True),
        },
    )
    anchor = SimpleNamespace(
        detection_id="det-1",
        anchor_type=SimpleNamespace(value="ema20_50_cross"),
        outcome=outcome,
    )
    return SimpleNamespace(
        journey_id="journey-abcdef123456",
        symbol="XAUUSD",
        direction=Direction.BUY,
        end_reason=SimpleNamespace(value="max_horizon"),
        anchors=(anchor,),
    )


def test_one_thread_is_created_and_reused_for_one_journey() -> None:
    context = FakeContext()
    sends: list[tuple[object, object]] = []
    threads: list[tuple[object, str, str]] = []

    async def send(target, *, embed=None, **kwargs):
        sends.append((target, embed))
        return "111"

    async def create_thread(target, message_id, *, name):
        threads.append((target, message_id, name))
        return "999"

    notifier = Notifier(
        context,
        send=send,
        create_thread=create_thread,
        charts=False,
    )
    journey = _journey()
    now = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)

    first = asyncio.run(notifier._ensure_journey_thread(journey, now=now))
    second = asyncio.run(notifier._ensure_journey_thread(journey, now=now))

    assert first == "999"
    assert second == "999"
    assert len(threads) == 1
    assert sends[0][0] == 4242
    record = context.notifications.get(NotificationKind.JOURNEY, journey.journey_id)
    assert record.message_id == "999"


def test_closeout_posts_prediction_vs_actual_inside_journey_thread() -> None:
    context = FakeContext()
    posted: list[tuple[object, object]] = []

    async def send(target, *, embed=None, **kwargs):
        posted.append((target, embed))
        return "222"

    async def create_thread(target, message_id, *, name):
        return "999"

    notifier = Notifier(
        context,
        send=send,
        create_thread=create_thread,
        charts=False,
    )
    journey = _journey()
    now = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)
    asyncio.run(notifier._ensure_journey_thread(journey, now=now))
    posted.clear()

    ok = asyncio.run(
        notifier._post_journey_outcome(
            journey,
            now=now,
            show_model_confidence=True,
        )
    )

    assert ok is True
    assert len(posted) == 1
    assert posted[0][0] == "999"
    embed = posted[0][1]
    text = "\n".join(
        [embed.title or "", embed.description or ""]
        + [field.value for field in embed.fields]
    )
    assert "P(+3)" in text
    assert "82% (n=90)" in text
    assert "MFE 12.00" in text
    assert "End reason: max_horizon" in text


def test_model_confidence_kill_switch_hides_predictions_from_closeout() -> None:
    context = FakeContext()
    posted: list[tuple[object, object]] = []

    async def send(target, *, embed=None, **kwargs):
        posted.append((target, embed))
        return "222"

    notifier = Notifier(context, send=send, charts=False)
    journey = _journey()
    now = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)

    ok = asyncio.run(
        notifier._post_journey_outcome(
            journey,
            now=now,
            show_model_confidence=False,
        )
    )

    assert ok is True
    embed = posted[0][1]
    text = "\n".join(
        [embed.title or "", embed.description or ""]
        + [field.value for field in embed.fields]
    )
    assert "P(+3)" not in text
    assert "actual" in text.lower()

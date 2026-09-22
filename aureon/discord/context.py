"""What the Discord layer is allowed to reach (§71, CLAUDE.md).

A single object holding every repository the bot may use -- and, just as importantly, not
holding a broker or a data provider. Discord reads Firestore and writes only
``trade_requests``, ``control_requests``, ``settings.trading_enabled`` and ``audit_logs``.

Firestore's Python client is **synchronous**. Called directly from a discord.py handler it
would block the event loop, and a blocked loop means missed heartbeats and interactions
that time out at three seconds. So every call goes through ``run`` , which pushes it to a
worker thread.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from aureon.config import AureonConfig
from aureon.models.settings import ExecutionSettings
from aureon.storage.alert_repository import PriceAlertRepository
from aureon.storage.assessment_repository import AssessmentRepository
from aureon.storage.control_request_repository import ControlRequestRepository
from aureon.storage.detection_repository import DetectionRepository
from aureon.storage.evaluation_reader import EvaluationReader
from aureon.storage.note_repository import TradeNoteRepository
from aureon.storage.notification_repository import NotificationRepository
from aureon.storage.ops_repository import OpsEventRepository
from aureon.storage.review_reader import ReviewReader
from aureon.storage.settings_repository import (
    ExecutionSettingsRepository,
    NotificationSettingsRepository,
)
from aureon.storage.setup_reader import MarketDayReader, SetupReader
from aureon.storage.symbol_repository import SymbolRepository
from aureon.storage.system_state_repository import HeartbeatRepository, SystemStateRepository
from aureon.storage.trade_repository import TradeRepository
from aureon.storage.trade_request_repository import TradeRequestRepository

log = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass
class BotContext:
    """Everything a command handler needs.

    Deliberately has no ``broker`` and no ``provider`` field: there is nothing here to
    place an order with, so Discord cannot trade even by mistake.
    """

    config: AureonConfig
    requests: TradeRequestRepository
    controls: ControlRequestRepository
    trades: TradeRepository
    detections: DetectionRepository
    settings: ExecutionSettingsRepository
    # Deliberately the READER: Discord must not be able to overwrite a review (§61).
    reviews: ReviewReader
    symbols: SymbolRepository
    system_state: SystemStateRepository
    heartbeats: HeartbeatRepository
    # 9C: the alerts a human armed, and what has already been said. Both are writable by
    # Discord -- an alert IS a Discord artefact, and a notification record is the proof a
    # message was sent -- which is why they sit beside `requests` rather than behind a
    # reader like `reviews` does.
    alerts: PriceAlertRepository | None = None
    notifications: NotificationRepository | None = None
    #: Which agents are announced (9C). Its own repository, because the document that
    #: decides whether a message is posted should not be reachable from the one that
    #: decides whether money can move.
    notification_settings: NotificationSettingsRepository | None = None
    #: 9D. Deliberately the READER: `/monitor` counts stored outcomes and must not be able
    #: to rewrite one, or every number built on them stops being falsifiable. The boundary
    #: test caught the writing repository here on its first outing. Assessments and notes
    #: ARE Discord artefacts -- `/monitor` and `/note` are the only things that create one --
    #: so those are full repositories, beside `alerts`.
    evaluations: EvaluationReader | None = None
    assessments: AssessmentRepository | None = None
    notes: TradeNoteRepository | None = None
    #: 11A F-15. Writable, because Discord raises no conditions of its own but `/ops` is the
    #: only place they are read, and a reader-only wrapper for a collection nothing in Discord
    #: writes would be ceremony rather than a boundary.
    ops_events: OpsEventRepository | None = None
    #: 12 T-11. Both deliberately READERS. ``setups`` is the observer's exclusively -- §71 does
    #: not permit Discord to write it, and the transaction in ``SetupRepository.record`` is safe
    #: precisely because one process writes. The day frames are the same: a chart reads bars, and
    #: a bot that could rewrite one could make its own picture agree with itself.
    setups: SetupReader | None = None
    market_days: MarketDayReader | None = None

    @property
    def authorized_user_ids(self) -> tuple[str, ...]:
        return self.config.authorized_user_ids

    async def run(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Run a blocking Firestore call off the event loop.

        Not an optimisation: discord.py must answer an interaction within three seconds,
        and a synchronous Firestore round trip on the loop can miss that and lose the
        interaction entirely.
        """
        return await asyncio.to_thread(fn, *args, **kwargs)

    async def execution_settings(self) -> ExecutionSettings:
        """Current settings, failing closed if they cannot be read (decision 66)."""
        return await self.run(self.settings.read_or_default)


def build_context(config: AureonConfig, client: Any) -> BotContext:
    """Assemble a context from a Firestore client."""
    return BotContext(
        config=config,
        requests=TradeRequestRepository(client),
        controls=ControlRequestRepository(client),
        trades=TradeRepository(client, account_scope=config.account_scope),
        detections=DetectionRepository(client),
        settings=ExecutionSettingsRepository(client),
        reviews=ReviewReader(client),
        symbols=SymbolRepository(client),
        system_state=SystemStateRepository(client),
        heartbeats=HeartbeatRepository(client),
        alerts=PriceAlertRepository(client),
        notifications=NotificationRepository(client),
        notification_settings=NotificationSettingsRepository(client),
        evaluations=EvaluationReader(client),
        assessments=AssessmentRepository(client),
        notes=TradeNoteRepository(client),
        ops_events=OpsEventRepository(client),
    )

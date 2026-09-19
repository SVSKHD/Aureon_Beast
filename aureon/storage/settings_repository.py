"""Runtime execution settings at ``settings/execution`` (§56, §57).

Decision 11: Firestore overrides the environment at runtime, so ``/trading disable``
takes effect on the very next request without a redeploy.

``read_or_default`` **fails closed**. A missing or unreadable settings document yields
``ExecutionSettings()``, whose ``trading_enabled`` is ``False`` -- so a fresh deployment,
or one whose settings document was lost, cannot trade until a human enables it. The
opposite default would turn an absent document into an open trading bot.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from aureon.models.audit import AuditRecord
from aureon.models.base import to_utc, utc_now
from aureon.models.settings import ExecutionSettings
from aureon.storage import paths

log = logging.getLogger(__name__)


class ExecutionSettingsRepository:
    """Reads and writes the single ``settings/execution`` document."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def read(self) -> ExecutionSettings | None:
        snapshot = self._client.document(paths.execution_settings_path()).get()
        if not getattr(snapshot, "exists", False):
            return None
        return ExecutionSettings.model_validate(snapshot.to_dict())

    def read_or_default(self) -> ExecutionSettings:
        """Settings, or the fail-closed default if they cannot be read."""
        try:
            settings = self.read()
        except Exception:  # noqa: BLE001
            log.exception("could not read %s; failing closed", paths.execution_settings_path())
            return ExecutionSettings()
        return settings or ExecutionSettings()

    def write(self, settings: ExecutionSettings) -> ExecutionSettings:
        self._client.document(paths.execution_settings_path()).set(
            settings.model_dump(mode="json")
        )
        return settings

    def set_trading_enabled(
        self,
        enabled: bool,
        *,
        actor: str,
        reason: str | None = None,
        now: datetime | None = None,
    ) -> ExecutionSettings:
        """Flip the trading switch and audit it in the same breath (§57, §60).

        Disabling must take effect immediately, so this writes the document rather than
        queueing anything.
        """
        moment = to_utc(now or utc_now())
        current = self.read_or_default()
        updated = current.model_copy(
            update={
                "trading_enabled": enabled,
                "updated_at": moment,
                "updated_by": actor,
                "disabled_reason": None if enabled else reason,
            }
        )
        self.write(updated)

        import uuid

        record = AuditRecord(
            audit_id=uuid.uuid4().hex,
            at=moment,
            actor=actor,
            action="trading.enable" if enabled else "trading.disable",
            collection=paths.SETTINGS,
            document_id=paths.EXECUTION_SETTINGS_DOC,
            from_status=str(current.trading_enabled),
            to_status=str(enabled),
            reason=reason,
        )
        self._client.document(paths.audit_path(record.audit_id)).set(
            record.model_dump(mode="json")
        )
        return updated

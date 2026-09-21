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
from collections.abc import Callable
from datetime import datetime
from typing import Any

from aureon.models.audit import AuditRecord
from aureon.models.base import to_utc, utc_now
from aureon.models.settings import ExecutionSettings, NotificationSettings
from aureon.storage import paths

log = logging.getLogger(__name__)


class SettingsVersionConflict(RuntimeError):
    """A caller asserted a settings version that is no longer current."""


def audit_id_for_settings(version: int) -> str:
    """Deterministic audit id for one settings version (§60).

    Deterministic so a retried transaction re-writes the same row instead of leaving two
    audit entries for one toggle -- which would make the trail claim two decisions where
    a human made one.
    """
    return f"settings-execution-v{version:08d}"


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

    @staticmethod
    def _run(transaction: Any, fn: Callable[..., Any], *args: Any) -> Any:
        from google.cloud import firestore

        return firestore.transactional(fn)(transaction, *args)

    def _transaction(self) -> Any:
        return self._client.transaction()

    def set_trading_enabled(
        self,
        enabled: bool,
        *,
        actor: str,
        reason: str | None = None,
        now: datetime | None = None,
        if_version: int | None = None,
    ) -> ExecutionSettings:
        """Flip the trading switch and audit it, in ONE transaction (§57, §60).

        Two things had to change here, and both are about the same thing: this is the
        kill switch, so a write that half-happens is worse than one that fails.

        * **One transaction.** The settings write and its ``AuditRecord`` commit
          together. Separately, the switch could flip with no audit row -- a gap in the
          §60 trail on the one setting that decides whether real money can move -- or an
          audit row could land for a write that failed, which is worse, because it
          claims something happened that did not.
        * **A version check.** The transaction reads ``settings_version`` and writes only
          if it is still what it read. Two concurrent toggles therefore resolve to
          exactly one winner; Firestore aborts the loser because its read set changed.
          Without it, a disable racing an enable is last-write-wins, and the direction
          that loses might be the safe one.

        ``if_version`` lets a caller assert what it believed it was changing. Passing a
        stale value raises ``SettingsVersionConflict`` rather than overwriting -- for a
        UI that read the settings, rendered a button, and got a press seconds later.
        """
        moment = to_utc(now or utc_now())

        def txn(transaction: Any) -> ExecutionSettings:
            ref = self._client.document(paths.execution_settings_path())
            snapshot = ref.get(transaction=transaction)
            if getattr(snapshot, "exists", False):
                current = ExecutionSettings.model_validate(snapshot.to_dict())
            else:
                # Fail-closed default, same as read_or_default: an absent document must
                # not read as "trading was already on".
                current = ExecutionSettings()

            if if_version is not None and current.settings_version != if_version:
                raise SettingsVersionConflict(
                    f"settings are at version {current.settings_version}, not "
                    f"{if_version}; something changed them since you read them"
                )

            updated = current.model_copy(
                update={
                    "trading_enabled": enabled,
                    "settings_version": current.settings_version + 1,
                    "updated_at": moment,
                    "updated_by": actor,
                    "disabled_reason": None if enabled else reason,
                }
            )
            transaction.set(ref, updated.model_dump(mode="json"))

            record = AuditRecord(
                # Deterministic, not random: the id is a function of what changed, so a
                # transaction Firestore retries writes the same audit row rather than
                # leaving two rows for one human decision.
                audit_id=audit_id_for_settings(updated.settings_version),
                at=moment,
                actor=actor,
                action="trading.enable" if enabled else "trading.disable",
                collection=paths.SETTINGS,
                document_id=paths.EXECUTION_SETTINGS_DOC,
                from_status=str(current.trading_enabled),
                to_status=str(enabled),
                reason=reason,
            )
            transaction.set(
                self._client.document(paths.audit_path(record.audit_id)),
                record.model_dump(mode="json"),
            )
            return updated

        return self._run(self._transaction(), txn)


class NotificationSettingsRepository:
    """``settings/notifications`` — which agents Discord announces (9C).

    Its own class rather than a method on ``ExecutionSettingsRepository`` because the two
    documents have different blast radii: one decides whether real money can move, the other
    whether a message is posted. Holding them apart means a surface that only needs the
    second cannot reach the first.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    def read_or_default(self) -> NotificationSettings:
        """The stored settings, or the shipped defaults.

        Defaults rather than silence on a missing document: a deployment that has never
        written this document should still announce the four obvious agents, and an operator
        who wants silence has `detections_enabled`.
        """
        snapshot = self._client.document(paths.notification_settings_path()).get()
        if not getattr(snapshot, "exists", False):
            return NotificationSettings()
        return NotificationSettings.model_validate(snapshot.to_dict())

    def write(self, settings: NotificationSettings) -> NotificationSettings:
        self._client.document(paths.notification_settings_path()).set(
            settings.model_dump(mode="json")
        )
        return settings

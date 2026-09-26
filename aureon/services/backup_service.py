"""Local-first V1 backup and monthly recovery snapshots.

Trading never calls this service. A scheduler/operator may invoke it asynchronously.
The optional Drive target is a filesystem path (for example a Google Drive Desktop
synced folder); Drive failure is logged and never propagates into Aureon's hot path.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class BackupResult:
    snapshot_dir: Path
    manifest_path: Path
    files: int
    drive_synced: int = 0
    drive_failed: bool = False


class BackupService:
    """Create immutable local snapshots and optionally mirror changed files to Drive."""

    def __init__(
        self,
        *,
        backup_root: str | Path = "backups",
        drive_root: str | Path | None = None,
    ) -> None:
        self.backup_root = Path(backup_root)
        self.drive_root = Path(drive_root) if drive_root else None

    def monthly_snapshot(
        self,
        *,
        month: str,
        assets: Iterable[str | Path],
        config_snapshot: dict | None = None,
    ) -> BackupResult:
        """Create backups/YYYY-MM once; existing bytes are never silently replaced."""
        if len(month) != 7 or month[4] != "-":
            raise ValueError("month must be YYYY-MM")
        root = self.backup_root / month
        root.mkdir(parents=True, exist_ok=True)
        records: list[dict] = []

        for raw in assets:
            source = Path(raw)
            if not source.exists():
                continue
            category = self._category(source)
            destination = root / category / source.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                # Immutable snapshot: only accept an identical repeated run.
                if sha256_file(destination) != sha256_file(source):
                    raise FileExistsError(
                        f"immutable monthly snapshot already contains changed {destination}"
                    )
            else:
                self._safe_copy(source, destination)
            stat = destination.stat()
            records.append(
                {
                    "source": str(source),
                    "path": str(destination.relative_to(root)),
                    "sha256": sha256_file(destination),
                    "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                }
            )

        if config_snapshot is not None:
            config_path = root / "config" / "config_snapshot.json"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            encoded = json.dumps(config_snapshot, indent=2, sort_keys=True).encode("utf-8")
            if config_path.exists() and config_path.read_bytes() != encoded:
                raise FileExistsError(
                    "immutable monthly snapshot already has a different config snapshot"
                )
            if not config_path.exists():
                config_path.write_bytes(encoded)
            records.append(
                {
                    "source": "runtime-config",
                    "path": str(config_path.relative_to(root)),
                    "sha256": sha256_file(config_path),
                    "size": config_path.stat().st_size,
                    "mtime_ns": config_path.stat().st_mtime_ns,
                }
            )

        manifest = {
            "schema": "AUREON_BACKUP_MANIFEST_V1",
            "month": month,
            "created_at": datetime.now(UTC).isoformat(),
            "files": sorted(records, key=lambda item: item["path"]),
        }
        manifest_path = root / "manifest.json"
        # Manifest creation time is operational metadata, so repeated snapshot runs may
        # refresh only the manifest while asset bytes remain immutable.
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True),
            encoding="utf-8",
        )

        synced = 0
        failed = False
        if self.drive_root is not None:
            try:
                synced = self.sync_changed_to_drive(root)
            except Exception:
                failed = True
                log.warning(
                    "Google Drive backup unavailable; local snapshot is complete and Aureon "
                    "continues normally",
                    exc_info=True,
                )
        return BackupResult(
            snapshot_dir=root,
            manifest_path=manifest_path,
            files=len(records),
            drive_synced=synced,
            drive_failed=failed,
        )

    def sync_changed_to_drive(self, snapshot_dir: Path) -> int:
        """Mirror only missing/changed files into the optional Drive folder."""
        if self.drive_root is None:
            return 0
        relative = snapshot_dir.relative_to(self.backup_root)
        target_root = self.drive_root / "Aureon_Beast" / "backups" / relative
        copied = 0
        for source in snapshot_dir.rglob("*"):
            if not source.is_file():
                continue
            destination = target_root / source.relative_to(snapshot_dir)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if (
                destination.exists()
                and destination.stat().st_size == source.stat().st_size
                and sha256_file(destination) == sha256_file(source)
            ):
                continue
            shutil.copy2(source, destination)
            copied += 1
        return copied

    @staticmethod
    def _safe_copy(source: Path, destination: Path) -> None:
        if source.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
            # SQLite's backup API produces a transactionally consistent copy even in WAL mode.
            source_connection = sqlite3.connect(str(source))
            destination_connection = sqlite3.connect(str(destination))
            try:
                source_connection.backup(destination_connection)
            finally:
                destination_connection.close()
                source_connection.close()
            return
        shutil.copy2(source, destination)

    @staticmethod
    def _category(path: Path) -> str:
        lowered = str(path).lower()
        if "backtest" in lowered:
            return "backtests"
        if "model" in lowered:
            return "models"
        if "training" in lowered or path.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
            return "training"
        if "evolution" in lowered:
            return "evolution"
        if "config" in lowered or path.suffix.lower() in {".env", ".toml", ".yaml", ".yml"}:
            return "config"
        return "artifacts"

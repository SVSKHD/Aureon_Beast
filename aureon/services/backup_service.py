"""Local-first backup and immutable monthly recovery snapshots.

Google Drive is represented by an optional locally mounted/synced Drive directory. Aureon
never imports a cloud SDK or waits for network I/O in its market/execution processes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import sqlite3
import threading
from dataclasses import dataclass, asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BackupReport:
    ok: bool
    copied: int = 0
    unchanged: int = 0
    failed: int = 0
    message: str = ""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_config(payload: dict[str, Any]) -> dict[str, Any]:
    secret_words = ("password", "token", "secret", "credential", "authorization")
    return {
        key: ("<redacted>" if any(word in key.lower() for word in secret_words) else value)
        for key, value in payload.items()
    }


class BackupService:
    """Best-effort backup service. Failures are returned/logged, not raised by sync()."""

    def __init__(
        self,
        *,
        local_root: str | Path = "data/backups",
        drive_root: str | Path | None = None,
        sqlite_path: str | Path = "data/aureon.db",
        artifact_roots: Iterable[str | Path] = ("data/backtests",),
    ) -> None:
        self.local_root = Path(local_root)
        self.drive_root = Path(drive_root).expanduser() if drive_root else None
        self.sqlite_path = Path(sqlite_path)
        self.artifact_roots = tuple(Path(one) for one in artifact_roots)
        self.manifest_path = self.local_root / "sync_manifest.json"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def prepare_operational_snapshot(
        self,
        *,
        config: dict[str, Any] | None = None,
    ) -> Path:
        """Create a consistent local SQLite backup plus redacted config snapshot."""
        staging = self.local_root / "current"
        staging.mkdir(parents=True, exist_ok=True)
        if self.sqlite_path.exists():
            destination = staging / "aureon.db"
            source = sqlite3.connect(str(self.sqlite_path))
            target = sqlite3.connect(str(destination))
            try:
                source.backup(target)
            finally:
                target.close()
                source.close()
        if config is not None:
            (staging / "config.json").write_text(
                json.dumps(_safe_config(config), indent=2, sort_keys=True, default=str),
                encoding="utf-8",
            )
        artifacts = staging / "artifacts"
        artifacts.mkdir(exist_ok=True)
        for root in self.artifact_roots:
            if not root.exists():
                continue
            target = artifacts / root.name
            if target.exists():
                shutil.rmtree(target)
            if root.is_dir():
                shutil.copytree(root, target)
            else:
                shutil.copy2(root, target)
        return staging

    def sync_changed(self, *, source: Path | None = None) -> BackupReport:
        """Copy changed files to Drive target using checksum manifest. Never raises."""
        if self.drive_root is None:
            return BackupReport(ok=True, message="Drive backup disabled: no target directory")
        source = source or (self.local_root / "current")
        try:
            if not source.exists():
                return BackupReport(ok=False, failed=1, message=f"source missing: {source}")
            self.drive_root.mkdir(parents=True, exist_ok=True)
            manifest = self._load_manifest()
            new_manifest: dict[str, Any] = dict(manifest)
            copied = unchanged = 0
            for path in sorted(one for one in source.rglob("*") if one.is_file()):
                relative = path.relative_to(source).as_posix()
                checksum = sha256_file(path)
                size = path.stat().st_size
                old = manifest.get(relative) or {}
                destination = self.drive_root / relative
                if (
                    old.get("sha256") == checksum
                    and old.get("size") == size
                    and destination.exists()
                ):
                    unchanged += 1
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, destination)
                new_manifest[relative] = {
                    "sha256": checksum,
                    "size": size,
                    "synced_at": datetime.now(UTC).isoformat(),
                }
                copied += 1
            self._write_manifest(new_manifest)
            return BackupReport(ok=True, copied=copied, unchanged=unchanged)
        except Exception as exc:  # cloud/offsite failure never stops Aureon
            log.warning("Drive backup failed; Aureon remains local-first: %s", exc)
            return BackupReport(ok=False, failed=1, message=str(exc))

    def monthly_snapshot(
        self,
        month: str,
        *,
        config: dict[str, Any] | None = None,
    ) -> BackupReport:
        """Create an immutable YYYY-MM snapshot with checksums, then optionally sync it."""
        if len(month) != 7 or month[4] != "-":
            raise ValueError("month must be YYYY-MM")
        try:
            current = self.prepare_operational_snapshot(config=config)
            destination = self.local_root / month
            if destination.exists():
                # Immutable means a second invocation validates, not overwrites.
                manifest = destination / "manifest.json"
                if not manifest.exists():
                    raise RuntimeError(f"snapshot {month} exists without manifest")
                return BackupReport(ok=True, unchanged=1, message="snapshot already immutable")

            (destination / "models").mkdir(parents=True, exist_ok=False)
            (destination / "training").mkdir(exist_ok=True)
            (destination / "evolution").mkdir(exist_ok=True)
            (destination / "config").mkdir(exist_ok=True)
            (destination / "artifacts").mkdir(exist_ok=True)

            db = current / "aureon.db"
            if db.exists():
                # One consistent DB contains training memory, registry, predictions and evolution.
                shutil.copy2(db, destination / "training" / "aureon.db")
            cfg = current / "config.json"
            if cfg.exists():
                shutil.copy2(cfg, destination / "config" / "config.json")
            current_artifacts = current / "artifacts"
            if current_artifacts.exists():
                for item in current_artifacts.iterdir():
                    target = destination / "artifacts" / item.name
                    if item.is_dir():
                        shutil.copytree(item, target)
                    else:
                        shutil.copy2(item, target)

            entries: dict[str, Any] = {}
            for path in sorted(one for one in destination.rglob("*") if one.is_file()):
                if path.name == "manifest.json":
                    continue
                relative = path.relative_to(destination).as_posix()
                entries[relative] = {
                    "sha256": sha256_file(path),
                    "size": path.stat().st_size,
                }
            manifest_payload = {
                "schema": "AUREON_MONTHLY_BACKUP_V1",
                "month": month,
                "created_at": datetime.now(UTC).isoformat(),
                "files": entries,
            }
            (destination / "manifest.json").write_text(
                json.dumps(manifest_payload, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            if self.drive_root is not None:
                return self.sync_changed(source=destination)
            return BackupReport(ok=True, copied=len(entries) + 1)
        except Exception as exc:
            log.warning("monthly backup failed without affecting Aureon: %s", exc)
            return BackupReport(ok=False, failed=1, message=str(exc))

    def start_periodic(
        self,
        *,
        interval_seconds: float,
        config_provider: Any | None = None,
    ) -> None:
        if interval_seconds < 60:
            raise ValueError("backup interval must be at least 60 seconds")
        if self._thread is not None and self._thread.is_alive():
            return

        def worker() -> None:
            while not self._stop.is_set():
                payload = None
                try:
                    if config_provider is not None:
                        raw = config_provider()
                        payload = (
                            raw.model_dump(mode="json")
                            if hasattr(raw, "model_dump")
                            else dict(raw)
                        )
                    self.prepare_operational_snapshot(config=payload)
                    self.sync_changed()
                except Exception:
                    log.warning("periodic backup cycle failed", exc_info=True)
                self._stop.wait(interval_seconds)

        self._stop.clear()
        self._thread = threading.Thread(
            target=worker,
            name="aureon-backup",
            daemon=True,
        )
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _load_manifest(self) -> dict[str, Any]:
        if not self.manifest_path.exists():
            return {}
        return json.loads(self.manifest_path.read_text(encoding="utf-8"))

    def _write_manifest(self, payload: dict[str, Any]) -> None:
        self.local_root.mkdir(parents=True, exist_ok=True)
        temp = self.manifest_path.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        temp.replace(self.manifest_path)

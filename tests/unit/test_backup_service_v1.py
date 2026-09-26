"""Local-first backup tests."""

import json

from aureon.services.backup_service import BackupService, sha256_file


class FailingDriveBackup(BackupService):
    def sync_changed_to_drive(self, snapshot_dir):
        raise OSError("drive offline")


def test_monthly_snapshot_writes_checksum_manifest(tmp_path) -> None:
    source = tmp_path / "training.json"
    source.write_text('{"x": 1}', encoding="utf-8")
    service = BackupService(backup_root=tmp_path / "backups")

    result = service.monthly_snapshot(
        month="2026-09",
        assets=[source],
        config_snapshot={"symbol": "XAUUSD"},
    )

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema"] == "AUREON_BACKUP_MANIFEST_V1"
    rows = {row["path"]: row for row in manifest["files"]}
    copied = result.snapshot_dir / "training" / source.name
    assert copied.exists()
    assert rows[f"training/{source.name}"]["sha256"] == sha256_file(copied)
    assert (result.snapshot_dir / "config" / "config_snapshot.json").exists()


def test_drive_failure_does_not_fail_local_snapshot(tmp_path) -> None:
    source = tmp_path / "model.json"
    source.write_text('{"model": "champion"}', encoding="utf-8")
    service = FailingDriveBackup(
        backup_root=tmp_path / "backups",
        drive_root=tmp_path / "drive",
    )

    result = service.monthly_snapshot(month="2026-09", assets=[source])
    thread = service.sync_drive_async(result.snapshot_dir)
    thread.join(timeout=2)

    assert result.manifest_path.exists()
    assert result.files == 1

"""Local-first backup and Drive-failure tests."""

import json

from aureon.services.backup_service import BackupService, sha256_file


def test_monthly_snapshot_manifest_contains_checksum(tmp_path) -> None:
    source = tmp_path / "model.json"
    source.write_text('{"model":"champion"}', encoding="utf-8")
    service = BackupService(backup_root=tmp_path / "backups")
    result = service.monthly_snapshot(month="2026-09", assets=[source])
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema"] == "AUREON_BACKUP_MANIFEST_V1"
    assert manifest["files"][0]["sha256"] == sha256_file(source)
    assert result.files == 1


def test_drive_failure_does_not_break_local_snapshot(tmp_path, monkeypatch) -> None:
    source = tmp_path / "training.json"
    source.write_text("memory", encoding="utf-8")
    service = BackupService(
        backup_root=tmp_path / "backups", drive_root=tmp_path / "drive"
    )
    result = service.monthly_snapshot(month="2026-09", assets=[source])

    def fail(_snapshot):
        raise OSError("drive unavailable")

    monkeypatch.setattr(service, "sync_changed_to_drive", fail)
    thread = service.sync_drive_async(result.snapshot_dir)
    thread.join(timeout=2)
    assert result.manifest_path.exists()
    assert result.manifest_path.read_text(encoding="utf-8")

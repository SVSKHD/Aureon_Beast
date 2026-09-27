"""GAP 12/13: manifest verification, Drive isolation, and the single health report."""

from __future__ import annotations

from pathlib import Path

from aureon.services.backup_service import BackupService, verify_manifest
from aureon.services.v1_health import build_v1_health_report, render_health
from aureon.storage.postgres.repositories.models import ModelRepository
from tests.unit.v1_fixtures import constant_model


def test_manifest_checksum_verification_detects_a_changed_file(tmp_path: Path) -> None:
    asset = tmp_path / "training_examples.json"
    asset.write_text("[1,2,3]", encoding="utf-8")
    service = BackupService(backup_root=tmp_path / "backups")
    result = service.monthly_snapshot(month="2026-02", assets=[asset], config_snapshot={"a": 1})
    verification = verify_manifest(result.snapshot_dir)
    assert verification.ok and verification.files == 2
    copied = next(result.snapshot_dir.rglob("training_examples.json"))
    copied.write_text("[9]", encoding="utf-8")
    tampered = verify_manifest(result.snapshot_dir)
    assert not tampered.ok and any("checksum mismatch" in p for p in tampered.problems)
    assert not verify_manifest(tmp_path / "nowhere").ok


def test_sqlite_snapshot_is_transactionally_consistent(tmp_path: Path, local_store) -> None:
    models = ModelRepository(local_store)
    models.write_model(constant_model("m-1"))
    service = BackupService(backup_root=tmp_path / "backups")
    result = service.monthly_snapshot(month="2026-03", assets=[Path(local_store.path)])
    copy = next(result.snapshot_dir.rglob("*.db"))
    import sqlite3

    connection = sqlite3.connect(str(copy))
    try:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT count(*) FROM model_registry").fetchone()[0] == 1
    finally:
        connection.close()
    assert verify_manifest(result.snapshot_dir).ok


def test_drive_failure_is_recorded_and_never_touches_the_local_snapshot(tmp_path: Path) -> None:
    asset = tmp_path / "models.json"
    asset.write_text("{}", encoding="utf-8")
    drive = tmp_path / "drive_file_not_dir"
    drive.write_text("I am a file, not a folder", encoding="utf-8")
    service = BackupService(backup_root=tmp_path / "backups", drive_root=drive)
    result = service.monthly_snapshot(month="2026-04", assets=[asset])
    thread = service.sync_drive_async(result.snapshot_dir)
    thread.join(timeout=10)
    status = (result.snapshot_dir / "drive_sync_status.json").read_text(encoding="utf-8")
    assert '"failed"' in status
    assert verify_manifest(result.snapshot_dir).ok


def test_the_health_report_reads_every_section_from_storage(storage, tmp_path: Path) -> None:
    storage.models.write_model(constant_model("champ", clean=0.7))
    asset = tmp_path / "x.json"
    asset.write_text("{}", encoding="utf-8")
    BackupService(backup_root=tmp_path / "backups").monthly_snapshot(
        month="2026-05", assets=[asset]
    )
    report = build_v1_health_report(
        storage,
        symbols=("XAUUSD",),
        timeframe="M5",
        backup_root=tmp_path / "backups",
        autonomous_management_enabled=False,
    )
    assert report.learning["XAUUSD"].feature_schema == "AUREON_FEATURES_V1"
    assert report.learning["XAUUSD"].label_schema == "AUREON_CLEAN_MOVE_V1"
    assert report.model["XAUUSD"].champion == "champ"
    assert report.model_metrics["XAUUSD"].clean_10_precision == 0.7
    assert report.live_position.aureon_positions_open == 0
    assert (
        report.backup.latest_local_snapshot == "2026-05" and report.backup.manifest_verified is True
    )
    assert report.system.storage_health == "ok"
    assert report.system.autonomous_management_enabled is False
    text = render_health(report)
    for heading in (
        "MARKET",
        "AGENTS",
        "LEARNING",
        "MODEL",
        "LIVE POSITION",
        "EVOLUTION",
        "BACKUP",
        "SYSTEM",
    ):
        assert heading in text

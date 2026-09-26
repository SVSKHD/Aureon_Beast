"""Continuous V1 learning-cycle orchestration tests."""

from datetime import UTC, datetime

from aureon.services.learning_cycle import LearningCyclePolicy, LearningCycleService


class Memory:
    def __init__(self, examples):
        self.examples = list(examples)

    def canonical_between(self, symbol, start, end):
        return list(self.examples)


class Models:
    def __init__(self):
        self.shadow = None
        self.latest = None
        self.current_champion = None

    def active_shadow(self, symbol):
        return self.shadow

    def latest_model(self, symbol):
        return self.latest

    def champion(self, symbol):
        return self.current_champion


def test_learning_cycle_waits_for_canonical_samples() -> None:
    service = LearningCycleService(
        training_memory=Memory([]),
        models=Models(),
        symbols=("XAUUSD",),
        policy=LearningCyclePolicy(min_samples=30),
        now=lambda: datetime(2026, 9, 27, tzinfo=UTC),
    )

    result = service.run_once()[0]
    assert result.action == "wait_samples"
    assert result.canonical_examples == 0


def test_learning_cycle_failure_is_contained_per_symbol() -> None:
    class BrokenMemory:
        def canonical_between(self, symbol, start, end):
            raise OSError("local training read failed")

    service = LearningCycleService(
        training_memory=BrokenMemory(),
        models=Models(),
        symbols=("XAUUSD", "XAGUSD"),
        now=lambda: datetime(2026, 9, 27, tzinfo=UTC),
    )

    results = service.run_once()
    assert [one.action for one in results] == ["error", "error"]
    assert all("local training read failed" in one.detail for one in results)


def test_monthly_backup_is_not_rewritten_when_manifest_exists(tmp_path) -> None:
    class Backup:
        def __init__(self):
            self.backup_root = tmp_path / "backups"
            self.syncs = []

        def monthly_snapshot(self, **kwargs):
            raise AssertionError("existing monthly snapshot must not be overwritten")

        def sync_drive_async(self, snapshot_dir):
            self.syncs.append(snapshot_dir)
            return None

    backup = Backup()
    root = backup.backup_root / "2026-09"
    root.mkdir(parents=True)
    (root / "manifest.json").write_text("{}", encoding="utf-8")

    service = LearningCycleService(
        training_memory=Memory([]),
        models=Models(),
        symbols=("XAUUSD",),
        backup=backup,
        now=lambda: datetime(2026, 9, 27, tzinfo=UTC),
    )

    service.run_once()
    assert backup.syncs == [root]

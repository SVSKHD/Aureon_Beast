"""Tests for local pull/restart history used by /pull-history."""
from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

from aureon.services.pull_history import (
    read_restart_history,
    recent_pull_merges,
    record_restart,
    restart_includes_merge,
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "a.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "base")
    return repo


def test_recent_pull_merges_reads_main_first_parent_history(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "checkout", "-b", "feature")
    (repo / "a.txt").write_text("feature\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "feature")
    _git(repo, "checkout", "main")
    _git(repo, "merge", "--no-ff", "feature", "-m", "Merge pull request #42 from x/feature")

    merges = recent_pull_merges(repo, limit=5)

    assert len(merges) == 1
    assert merges[0].number == 42
    assert merges[0].sha == _git(repo, "rev-parse", "HEAD")


def test_record_restart_persists_latest_first(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    first = datetime(2026, 9, 30, 10, 0, tzinfo=UTC)
    second = datetime(2026, 9, 30, 11, 0, tzinfo=UTC)

    record_restart(
        repo,
        {"reason": "main branch updated", "new_sha": "abc"},
        restarted_at=first,
    )
    record_restart(
        repo,
        {"reason": "manual Discord restart", "new_sha": "def"},
        restarted_at=second,
    )

    records = read_restart_history(repo)
    assert [item.new_sha for item in records[:2]] == ["def", "abc"]
    assert records[0].restarted_at == second


def test_restart_includes_merge_when_new_sha_descends_from_merge(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "checkout", "-b", "feature")
    (repo / "a.txt").write_text("feature\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "feature")
    _git(repo, "checkout", "main")
    _git(repo, "merge", "--no-ff", "feature", "-m", "Merge pull request #7 from x/feature")
    merge_sha = _git(repo, "rev-parse", "HEAD")

    (repo / "b.txt").write_text("after\n", encoding="utf-8")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-m", "after merge")
    restarted_sha = _git(repo, "rev-parse", "HEAD")

    restart = record_restart(
        repo,
        {"reason": "main branch updated", "new_sha": restarted_sha},
        restarted_at=datetime(2026, 9, 30, 12, 0, tzinfo=UTC),
    )

    assert restart_includes_merge(repo, merge_sha, restart) is True

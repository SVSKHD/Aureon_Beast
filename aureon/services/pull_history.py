"""Local git/restart history used by the Discord /pull-history command."""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from aureon.models.base import to_utc, utc_now

HISTORY_FILENAME = "runtime_restart_history.json"
MAX_RESTART_HISTORY = 20
_PULL_RE = re.compile(r"Merge pull request #(\d+)\b")


@dataclass(frozen=True)
class PullMerge:
    number: int
    sha: str
    merged_at: datetime
    subject: str


@dataclass(frozen=True)
class RestartRecord:
    restarted_at: datetime
    reason: str
    detail: str
    old_sha: str | None = None
    new_sha: str | None = None


def restart_history_path(repo_root: Path) -> Path:
    return repo_root / "data" / HISTORY_FILENAME


def record_restart(
    repo_root: Path,
    notice: dict[str, Any],
    *,
    restarted_at: datetime | None = None,
) -> RestartRecord:
    """Persist one completed restart after the new Discord process is live."""
    record = RestartRecord(
        restarted_at=to_utc(restarted_at or utc_now()),
        reason=str(notice.get("reason") or "planned restart"),
        detail=str(notice.get("detail") or ""),
        old_sha=_optional_text(notice.get("old_sha")),
        new_sha=_optional_text(notice.get("new_sha")),
    )
    existing = read_restart_history(repo_root)
    payload = [
        {
            "restarted_at": item.restarted_at.isoformat(),
            "reason": item.reason,
            "detail": item.detail,
            "old_sha": item.old_sha,
            "new_sha": item.new_sha,
        }
        for item in [record, *existing][:MAX_RESTART_HISTORY]
    ]
    path = restart_history_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temp.replace(path)
    return record


def read_restart_history(repo_root: Path) -> list[RestartRecord]:
    try:
        payload = json.loads(restart_history_path(repo_root).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    if not isinstance(payload, list):
        return []

    records: list[RestartRecord] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        try:
            restarted_at = to_utc(datetime.fromisoformat(str(item["restarted_at"])))
        except (KeyError, ValueError, TypeError):
            continue
        records.append(
            RestartRecord(
                restarted_at=restarted_at,
                reason=str(item.get("reason") or "planned restart"),
                detail=str(item.get("detail") or ""),
                old_sha=_optional_text(item.get("old_sha")),
                new_sha=_optional_text(item.get("new_sha")),
            )
        )
    return records


def recent_pull_merges(repo_root: Path, *, limit: int = 5) -> list[PullMerge]:
    """Read recent PR merge commits from the local main first-parent history."""
    result = _git(
        repo_root,
        "log",
        "main",
        "--first-parent",
        "--merges",
        "-n",
        str(max(limit * 4, 20)),
        "--pretty=%H%x1f%cI%x1f%s",
    )
    if result.returncode != 0:
        return []

    merges: list[PullMerge] = []
    for line in result.stdout.splitlines():
        parts = line.split("\x1f", 2)
        if len(parts) != 3:
            continue
        sha, merged_at_raw, subject = parts
        match = _PULL_RE.search(subject)
        if match is None:
            continue
        try:
            merged_at = to_utc(datetime.fromisoformat(merged_at_raw))
        except ValueError:
            continue
        merges.append(
            PullMerge(
                number=int(match.group(1)),
                sha=sha,
                merged_at=merged_at,
                subject=subject,
            )
        )
        if len(merges) >= limit:
            break
    return merges


def current_main_sha(repo_root: Path) -> str | None:
    result = _git(repo_root, "rev-parse", "main")
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def restart_includes_merge(repo_root: Path, merge_sha: str, restart: RestartRecord) -> bool | None:
    """Whether the restarted SHA contains the selected main merge."""
    if not restart.new_sha:
        return None
    result = _git(repo_root, "merge-base", "--is-ancestor", merge_sha, restart.new_sha)
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    return None


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(
            ["git", *args],
            returncode=1,
            stdout="",
            stderr=str(exc),
        )


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None

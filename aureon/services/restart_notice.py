"""Durable hand-off message from the supervisor to Discord across a self-restart."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from aureon.models.base import to_utc, utc_now

NOTICE_FILENAME = "runtime_restart_notice.json"


def notice_path(repo_root: Path) -> Path:
    return repo_root / "data" / NOTICE_FILENAME


def write_restart_notice(
    repo_root: Path,
    *,
    reason: str,
    detail: str,
    old_sha: str | None = None,
    new_sha: str | None = None,
    now: datetime | None = None,
) -> None:
    path = notice_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "reason": reason,
        "detail": detail,
        "old_sha": old_sha,
        "new_sha": new_sha,
        "created_at": to_utc(now or utc_now()).isoformat(),
    }
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temp.replace(path)


def read_restart_notice(repo_root: Path) -> dict[str, Any] | None:
    path = notice_path(repo_root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def clear_restart_notice(repo_root: Path) -> None:
    try:
        notice_path(repo_root).unlink(missing_ok=True)
    except OSError:
        pass

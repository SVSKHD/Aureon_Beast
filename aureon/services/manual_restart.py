"""Durable manual restart request shared by Discord and the supervisor."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from aureon.models.base import to_utc, utc_now

REQUEST_FILENAME = "manual_restart_request.json"
MIN_ACK_SECONDS = 2.0


def request_path(repo_root: Path) -> Path:
    return repo_root / "data" / REQUEST_FILENAME


def request_restart(
    repo_root: Path,
    *,
    requested_by: str,
    reason: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    created = to_utc(now or utc_now())
    payload = {
        "requested_by": str(requested_by),
        "reason": reason.strip() or "Discord /restart",
        "created_at": created.isoformat(),
    }
    path = request_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temp.replace(path)
    return payload


def read_restart_request(repo_root: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(request_path(repo_root).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def consume_restart_request(repo_root: Path) -> dict[str, Any] | None:
    payload = read_restart_request(repo_root)
    if payload is None:
        return None
    try:
        created = to_utc(datetime.fromisoformat(str(payload.get("created_at"))))
    except (ValueError, TypeError):
        created = utc_now()
    if (utc_now() - created).total_seconds() < MIN_ACK_SECONDS:
        return None
    try:
        request_path(repo_root).unlink(missing_ok=True)
    except OSError:
        return None
    return payload

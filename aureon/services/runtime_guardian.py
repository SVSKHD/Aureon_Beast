"""Runtime guardian for safe self-updates and feed-liveness restarts.

This module deliberately sits OUTSIDE the trading decision path. It never creates detections,
trade requests, orders or confirmations. It only answers two operational questions:

1. Has origin/main advanced far enough that this checkout can fast-forward safely?
2. Has the market feed been silent long enough that a full-stack restart is warranted?

Expected weekend silence is never treated as a crash. A stale feed restart is also one-shot per
last-tick timestamp, so an outage cannot turn into an endless restart loop.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from aureon.config import AureonConfig
from aureon.models.base import to_utc, utc_now
from aureon.services.manual_restart import consume_restart_request
from aureon.services.market_state_service import WeeklySchedule
from aureon.services.restart_notice import write_restart_notice
from aureon.storage.runtime import build_storage

log = logging.getLogger("aureon.runtime_guardian")

DEFAULT_POLL_SECONDS = 120.0
DEFAULT_STALE_RESTART_SECONDS = 3 * 60 * 60
STATE_FILENAME = "runtime_guardian_state.json"


@dataclass(frozen=True)
class RestartRequest:
    """A planned full-stack restart, never a child crash."""

    reason: str
    detail: str


class RuntimeGuardian:
    """Poll git and persisted market state, returning a planned restart when needed."""

    def __init__(
        self,
        *,
        repo_root: Path,
        config: AureonConfig,
        storage: Any,
        ops: Any | None = None,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        stale_restart_seconds: float = DEFAULT_STALE_RESTART_SECONDS,
        auto_update_main: bool = False,
        auto_restart_stale_feed: bool = False,
        run_preflight: bool = True,
        now: Any = utc_now,
    ) -> None:
        self.repo_root = repo_root
        self.config = config
        self.storage = storage
        self.ops = ops
        self.poll_seconds = max(5.0, float(poll_seconds))
        self.stale_restart_seconds = max(60.0, float(stale_restart_seconds))
        self.auto_update_main = auto_update_main
        self.auto_restart_stale_feed = auto_restart_stale_feed
        self.run_preflight = run_preflight
        self._now = now
        self._next_poll = 0.0
        self._schedule = WeeklySchedule()
        self._state_path = self.repo_root / "data" / STATE_FILENAME
        self._last_holiday: dict[str, bool] = {}

    @classmethod
    def from_env(
        cls,
        *,
        repo_root: Path,
        ops: Any | None = None,
        run_preflight: bool = True,
    ) -> "RuntimeGuardian":
        config = AureonConfig.from_env()
        storage = build_storage(
            account_scope=config.account_scope,
            state_heartbeat_seconds=config.state_heartbeat_seconds,
        )
        return cls(
            repo_root=repo_root,
            config=config,
            storage=storage,
            ops=ops,
            poll_seconds=_env_float("AUREON_GUARDIAN_POLL_SECONDS", DEFAULT_POLL_SECONDS),
            stale_restart_seconds=_env_float(
                "AUREON_STALE_FEED_RESTART_SECONDS",
                DEFAULT_STALE_RESTART_SECONDS,
            ),
            auto_update_main=_env_bool("AUREON_AUTO_UPDATE_MAIN", False),
            auto_restart_stale_feed=_env_bool(
                "AUREON_AUTO_RESTART_STALE_FEED",
                False,
            ),
            run_preflight=run_preflight,
        )

    def diagnostics(self) -> dict[str, Any]:
        """Return the effective guardian state without changing git or restarting anything."""
        branch = ""
        dirty = ""
        local = ""
        remote = ""
        fetch_error = ""
        if (self.repo_root / ".git").exists():
            branch = self._git("branch", "--show-current", check=False).strip()
            dirty = self._git("status", "--porcelain", check=False).strip()
            local = self._git("rev-parse", "HEAD", check=False).strip()
            remote = self._git("rev-parse", "origin/main", check=False).strip()
            fetch = self._run_git("fetch", "--dry-run", "origin", "main", check=False)
            if fetch.returncode != 0:
                fetch_error = _one_line(fetch.stderr)

        return {
            "auto_update_main": self.auto_update_main,
            "auto_restart_stale_feed": self.auto_restart_stale_feed,
            "poll_seconds": self.poll_seconds,
            "stale_restart_seconds": self.stale_restart_seconds,
            "repo_root": str(self.repo_root),
            "git_checkout": (self.repo_root / ".git").exists(),
            "branch": branch,
            "working_tree_clean": not bool(dirty),
            "dirty_entries": dirty.splitlines()[:20] if dirty else [],
            "local_sha": local,
            "origin_main_sha": remote,
            "update_pending": bool(local and remote and local != remote),
            "fetch_error": fetch_error,
        }

    def log_startup_diagnostics(self) -> None:
        """Log the conditions that decide whether automatic updates are allowed."""
        status = self.diagnostics()
        log.info(
            "guardian status: auto_update_main=%s auto_restart_stale_feed=%s "
            "branch=%s clean=%s local=%s origin_main=%s update_pending=%s",
            status["auto_update_main"],
            status["auto_restart_stale_feed"],
            status["branch"] or "unknown",
            status["working_tree_clean"],
            str(status["local_sha"])[:12] or "unknown",
            str(status["origin_main_sha"])[:12] or "unknown",
            status["update_pending"],
        )
        if status["dirty_entries"]:
            log.warning(
                "guardian auto-update blocked by local changes: %s",
                " | ".join(status["dirty_entries"]),
            )
        if status["fetch_error"]:
            log.warning("guardian git fetch probe failed: %s", status["fetch_error"])

    def poll(self) -> RestartRequest | None:
        """Return a planned restart, checking manual Discord requests every supervisor tick."""

        manual = consume_restart_request(self.repo_root)
        if manual is not None:
            requested_by = str(manual.get("requested_by") or "unknown")
            reason = str(manual.get("reason") or "Discord /restart")
            detail = f"requested by Discord user {requested_by}: {reason}"
            write_restart_notice(self.repo_root, reason="manual Discord restart", detail=detail)
            log.warning("planned restart: %s", detail)
            return RestartRequest("manual Discord restart", detail)

        now_mono = time.monotonic()
        if now_mono < self._next_poll:
            return None
        self._next_poll = now_mono + self.poll_seconds

        update = self._check_main_update()
        if update is not None:
            return update
        return self._check_market_liveness()

    # ── main branch updates ──────────────────────────────────────────────────

    def _check_main_update(self) -> RestartRequest | None:
        if not self.auto_update_main:
            return None
        if not (self.repo_root / ".git").exists():
            log.warning("auto-update enabled but %s is not a git checkout", self.repo_root)
            return None

        branch = self._git("branch", "--show-current", check=False).strip()
        if branch != "main":
            log.warning(
                "auto-update skipped: checkout is on %r, not main; guardian never switches "
                "branches behind the operator's back",
                branch or "detached HEAD",
            )
            return None

        dirty = self._git("status", "--porcelain", check=False)
        if dirty.strip():
            log.warning("auto-update skipped: working tree has local changes")
            return None

        fetch = self._run_git("fetch", "--quiet", "origin", "main", check=False)
        if fetch.returncode != 0:
            log.warning("could not fetch origin/main: %s", _one_line(fetch.stderr))
            return None

        local = self._git("rev-parse", "HEAD", check=False).strip()
        remote = self._git("rev-parse", "origin/main", check=False).strip()
        if not local or not remote or local == remote:
            return None

        ancestor = self._run_git(
            "merge-base",
            "--is-ancestor",
            local,
            remote,
            check=False,
        )
        if ancestor.returncode != 0:
            log.warning(
                "origin/main is not a fast-forward from local main; refusing automatic merge "
                "(local=%s remote=%s)",
                local[:12],
                remote[:12],
            )
            return None

        subject = self._git(
            "log",
            "-1",
            "--pretty=%s",
            "origin/main",
            check=False,
        ).strip()
        merge = self._run_git("merge", "--ff-only", "origin/main", check=False)
        if merge.returncode != 0:
            log.error("fast-forward of main failed: %s", _one_line(merge.stderr))
            return None

        if self.run_preflight:
            preflight = subprocess.run(
                [sys.executable, "scripts/preflight.py"],
                cwd=self.repo_root,
                env=os.environ.copy(),
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
            if preflight.returncode != 0:
                log.error(
                    "new main failed preflight; rolling back to %s\n%s",
                    local[:12],
                    _tail(preflight.stdout + "\n" + preflight.stderr, 25),
                )
                rollback = self._run_git("reset", "--hard", local, check=False)
                if rollback.returncode != 0:
                    log.critical(
                        "automatic update rollback failed; manual intervention required: %s",
                        _one_line(rollback.stderr),
                    )
                return None

        detail = (
            f"origin/main advanced {local[:12]} -> {remote[:12]}"
            + (f" · {subject}" if subject else "")
        )
        write_restart_notice(
            self.repo_root,
            reason="main branch updated",
            detail=detail,
            old_sha=local,
            new_sha=remote,
        )
        log.warning("planned restart: %s", detail)
        return RestartRequest("main branch updated", detail)

    # ── market liveness ──────────────────────────────────────────────────────

    def _check_market_liveness(self) -> RestartRequest | None:
        now = to_utc(self._now())
        state_file = self._load_state()

        for symbol in self.config.symbols:
            state = self.storage.system_state.read_symbol(symbol, self.config.timeframes[0])
            tick_at = _last_tick_from_state(state)
            if tick_at is None:
                continue

            tick_at = to_utc(tick_at)
            age = max(0.0, (now - tick_at).total_seconds())
            expected_closed = not self._schedule.is_open(now)
            holiday = expected_closed and age >= self.stale_restart_seconds
            self._observe_holiday(symbol, holiday, age=age, tick_at=tick_at)

            if expected_closed:
                # Weekend silence is expected. Never restart because a Saturday/Sunday feed
                # remains on Friday's last quote.
                continue
            if age < self.stale_restart_seconds:
                continue
            if not self.auto_restart_stale_feed:
                log.warning(
                    "%s feed has been quiet %.1fh while market schedule says OPEN; "
                    "auto-restart is disabled",
                    symbol,
                    age / 3600.0,
                )
                continue

            previous_tick = state_file.get("stale_restart_tick_at", {}).get(symbol)
            current_tick = tick_at.isoformat()
            if previous_tick == current_tick:
                log.warning(
                    "%s is still on the same stale tick %s after a previous guardian restart; "
                    "suppressing another restart loop",
                    symbol,
                    current_tick,
                )
                continue

            per_symbol = dict(state_file.get("stale_restart_tick_at", {}))
            per_symbol[symbol] = current_tick
            state_file["stale_restart_tick_at"] = per_symbol
            self._save_state(state_file)

            detail = (
                f"{symbol} last tick {current_tick}; quiet for {age / 3600.0:.1f}h "
                "while weekly schedule says OPEN"
            )
            write_restart_notice(
                self.repo_root,
                reason="stale market feed",
                detail=detail,
            )
            log.error("planned restart: %s", detail)
            return RestartRequest("stale market feed", detail)

        return None

    def _observe_holiday(
        self,
        symbol: str,
        active: bool,
        *,
        age: float,
        tick_at: datetime,
    ) -> None:
        if self._last_holiday.get(symbol) == active and self.ops is None:
            return
        self._last_holiday[symbol] = active
        if self.ops is None:
            if active:
                log.info(
                    "%s market holiday confirmed: no tick for %.1fh and weekly schedule closed",
                    symbol,
                    age / 3600.0,
                )
            return
        try:
            self.ops.observe(
                "market_holiday",
                active=active,
                scope=symbol,
                detail=(
                    f"last tick {tick_at.isoformat()} · quiet {age / 3600.0:.1f}h · "
                    f"threshold {self.stale_restart_seconds / 3600.0:.1f}h"
                ),
            )
        except Exception:  # noqa: BLE001 - reporting cannot break supervision
            log.warning("could not record market holiday state", exc_info=True)

    # ── persistence / subprocess helpers ─────────────────────────────────────

    def _load_state(self) -> dict[str, Any]:
        try:
            return json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}

    def _save_state(self, state: dict[str, Any]) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            temp = self._state_path.with_suffix(".tmp")
            temp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
            temp.replace(self._state_path)
        except OSError:
            log.warning("could not persist runtime guardian state", exc_info=True)

    def _run_git(self, *args: str, check: bool) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                ["git", *args],
                cwd=self.repo_root,
                env=os.environ.copy(),
                capture_output=True,
                text=True,
                timeout=60,
                check=check,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return subprocess.CompletedProcess(
                ["git", *args],
                returncode=1,
                stdout="",
                stderr=str(exc),
            )

    def _git(self, *args: str, check: bool) -> str:
        return self._run_git(*args, check=check).stdout


def build_runtime_guardian(
    *,
    repo_root: Path,
    ops: Any | None = None,
    run_preflight: bool = True,
) -> RuntimeGuardian | None:
    """Build the guardian for manual /restart plus optional automatic behaviours.

    Manual Discord restart is always available, so the supervisor always owns a guardian.
    Auto-update and stale-feed restart remain opt-in.
    """
    guardian = RuntimeGuardian.from_env(
        repo_root=repo_root,
        ops=ops,
        run_preflight=run_preflight,
    )
    log.info(
        "runtime guardian enabled: auto_update_main=%s auto_restart_stale_feed=%s "
        "poll=%.0fs stale_restart=%.1fh",
        guardian.auto_update_main,
        guardian.auto_restart_stale_feed,
        guardian.poll_seconds,
        guardian.stale_restart_seconds / 3600.0,
    )
    guardian.log_startup_diagnostics()
    return guardian


def _last_tick_from_state(state: Any | None) -> datetime | None:
    if state is None or not getattr(state, "symbols", None):
        return None
    symbol_state = state.symbols[0]
    direct = getattr(symbol_state, "last_tick_at", None)
    if direct is not None:
        return direct
    quote = getattr(symbol_state, "last_quote", None)
    return getattr(quote, "captured_at", None) if quote is not None else None


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("%s=%r is not numeric; using %s", name, raw, default)
        return default


def _one_line(value: str) -> str:
    return " ".join((value or "").strip().split())[:500]


def _tail(value: str, lines: int) -> str:
    return "\n".join((value or "").splitlines()[-lines:])

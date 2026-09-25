#!/usr/bin/env python3
"""Show why Aureon's Runtime Guardian will or will not auto-update/restart.

This command is read-only: it does not merge, reset, restart services or touch trading state.

Examples:
    python scripts/guardian_status.py
    python scripts/guardian_status.py --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from aureon.services.supervisor import EnvFileError, load_env_file  # noqa: E402
from aureon.services.runtime_guardian import RuntimeGuardian  # noqa: E402


def _render(status: dict[str, object]) -> str:
    lines = [
        "Aureon Runtime Guardian",
        f"  auto_update_main: {status['auto_update_main']}",
        f"  auto_restart_stale_feed: {status['auto_restart_stale_feed']}",
        f"  poll_seconds: {status['poll_seconds']}",
        f"  stale_restart_seconds: {status['stale_restart_seconds']}",
        f"  repo_root: {status['repo_root']}",
        f"  git_checkout: {status['git_checkout']}",
        f"  branch: {status['branch'] or 'unknown'}",
        f"  working_tree_clean: {status['working_tree_clean']}",
        f"  local_sha: {str(status['local_sha'])[:12] or 'unknown'}",
        f"  origin_main_sha: {str(status['origin_main_sha'])[:12] or 'unknown'}",
        f"  update_pending: {status['update_pending']}",
    ]
    if status["dirty_entries"]:
        lines.append("  dirty_entries:")
        lines.extend(f"    {item}" for item in status["dirty_entries"])
    if status["fetch_error"]:
        lines.append(f"  fetch_error: {status['fetch_error']}")

    blockers = []
    if not status["auto_update_main"]:
        blockers.append("AUREON_AUTO_UPDATE_MAIN is not effectively true")
    if status["branch"] != "main":
        blockers.append("checkout is not on main")
    if not status["working_tree_clean"]:
        blockers.append("working tree has local changes")
    if status["fetch_error"]:
        blockers.append("git fetch cannot run successfully")
    if blockers:
        lines.append("")
        lines.append("AUTO-UPDATE BLOCKED:")
        lines.extend(f"  - {item}" for item in blockers)
    else:
        lines.append("")
        lines.append("AUTO-UPDATE ELIGIBLE")
        if status["update_pending"]:
            lines.append("  origin/main differs from local HEAD; guardian should update.")
        else:
            lines.append("  local HEAD already matches cached origin/main.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        load_env_file(Path(args.env_file))
    except (OSError, EnvFileError) as exc:
        print(f"FAIL cannot load {args.env_file}: {exc}", file=sys.stderr)
        return 2

    guardian = RuntimeGuardian.from_env(repo_root=REPO_ROOT, run_preflight=True)
    status = guardian.diagnostics()

    if args.json:
        print(json.dumps(status, indent=2, sort_keys=True))
    else:
        print(_render(status))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

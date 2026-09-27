#!/usr/bin/env python3
"""Print the single consolidated Aureon V1 health report (GAP 13).

python scripts/v1_status.py            # human-readable
python scripts/v1_status.py --json     # machine-readable
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from aureon.config import AureonConfig  # noqa: E402
from aureon.services.v1_health import build_v1_health_report, render_health  # noqa: E402
from aureon.storage.runtime import build_storage  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--backup-root", default=os.getenv("AUREON_BACKUP_ROOT", "backups"))
    args = parser.parse_args(argv)
    config = AureonConfig.from_env()
    storage = build_storage(
        account_scope=config.account_scope,
        state_heartbeat_seconds=config.state_heartbeat_seconds,
    )
    report = build_v1_health_report(
        storage,
        symbols=config.symbols,
        timeframe=config.timeframes[0],
        backup_root=args.backup_root,
        autonomous_management_enabled=config.autonomous_management_enabled,
        restart_notice_path=Path("data") / "runtime_restart_notice.json",
    )
    if args.json:
        print(json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True))
    else:
        print(render_health(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())

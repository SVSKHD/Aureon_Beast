#!/usr/bin/env python3
"""Create a local Aureon monthly snapshot and optionally mirror it to Google Drive Desktop."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from aureon.config import AureonConfig
from aureon.services.backup_service import BackupService
from aureon.storage.local_database import local_db_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--month", default=datetime.now(UTC).strftime("%Y-%m"))
    parser.add_argument("--backup-root", default="backups")
    parser.add_argument(
        "--drive-root",
        default=os.getenv("AUREON_GOOGLE_DRIVE_BACKUP_ROOT"),
        help="optional local path managed by Google Drive Desktop",
    )
    parser.add_argument(
        "--include",
        action="append",
        default=[],
        help="extra file to include; may be repeated",
    )
    args = parser.parse_args(argv)

    config = AureonConfig.from_env()
    assets = [local_db_path()]
    backtests = Path("data/backtests")
    if backtests.exists():
        assets.extend(path for path in backtests.glob("*.json") if path.is_file())
    assets.extend(Path(value) for value in args.include)

    # Secrets are deliberately omitted from backup configuration snapshots.
    safe_config = {
        "account_scope": config.account_scope,
        "market_tz": config.market_tz,
        "symbols": list(config.symbols),
        "timeframes": [timeframe.value for timeframe in config.timeframes],
        "ema_fast": config.ema_fast,
        "ema_slow": config.ema_slow,
        "ema_rsi_long_below": config.ema_rsi_long_below,
        "ema_rsi_short_above": config.ema_rsi_short_above,
        "evaluation_rule_id": config.evaluation_rule_id,
    }

    service = BackupService(
        backup_root=args.backup_root,
        drive_root=args.drive_root,
    )
    result = service.monthly_snapshot(
        month=args.month,
        assets=assets,
        config_snapshot=safe_config,
    )

    # This command is an offline maintenance job, never part of the trading hot path.
    # The service itself also exposes sync_drive_async() for long-running schedulers.
    drive_synced = 0
    drive_failed = False
    if args.drive_root:
        try:
            drive_synced = service.sync_changed_to_drive(result.snapshot_dir)
        except Exception as exc:  # noqa: BLE001 - offsite failure must not fail local backup
            drive_failed = True
            print(
                f"WARNING: Drive backup failed; local snapshot is safe: {exc}",
                file=sys.stderr,
            )
    print(
        json.dumps(
            {
                "snapshot": str(result.snapshot_dir),
                "manifest": str(result.manifest_path),
                "files": result.files,
                "drive_synced": drive_synced,
                "drive_failed": drive_failed,
            },
            indent=2,
        )
    )
    # A Drive failure is deliberately not a failure of the local snapshot command.
    return 0


if __name__ == "__main__":
    sys.exit(main())

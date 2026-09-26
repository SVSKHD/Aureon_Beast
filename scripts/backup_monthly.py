#!/usr/bin/env python3
"""Create one immutable local Aureon monthly snapshot and optionally sync Drive async."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from aureon.services.backup_service import BackupService
from aureon.storage.local_database import local_db_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--month", required=True, help="YYYY-MM")
    parser.add_argument("--backup-root", default="backups")
    parser.add_argument("--drive-root", default=os.getenv("AUREON_GOOGLE_DRIVE_BACKUP_ROOT"))
    parser.add_argument("--asset", action="append", default=[])
    parser.add_argument("--sync-drive", action="store_true")
    args = parser.parse_args(argv)

    assets = [Path(value) for value in args.asset]
    if not assets:
        assets = [local_db_path(), Path("data/backtests")]
    # Expand directories here so BackupService receives immutable file assets.
    expanded = []
    for asset in assets:
        if asset.is_dir():
            expanded.extend(path for path in asset.rglob("*") if path.is_file())
        elif asset.exists():
            expanded.append(asset)

    service = BackupService(
        backup_root=args.backup_root,
        drive_root=args.drive_root,
    )
    result = service.monthly_snapshot(month=args.month, assets=expanded)
    print(f"local snapshot: {result.snapshot_dir}")
    print(f"manifest: {result.manifest_path} files={result.files}")
    if args.sync_drive:
        thread = service.sync_drive_async(result.snapshot_dir)
        print(f"Drive sync started asynchronously: {thread.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

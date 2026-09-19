#!/usr/bin/env python3
"""Print reached-N counts from COMPLETE horizons only (Phase 3 "done when").

Thin wrapper over the backfill so the report and the data it reports on are produced
by one code path. Pending and invalid horizons are reported separately and are never
counted as misses.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scripts.backfill_evaluations import main as backfill_main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(backfill_main([*sys.argv[1:]]))

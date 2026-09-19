"""docs/PHASE2_BASELINE.md must match a replay of the committed fixture.

Phase 3 reconciles against these counts and Phase 7's weekly review must agree with
them, so a silent drift would be discovered several phases later as a mismatch nobody
could explain. This turns that into a failing test at the moment it happens.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_baseline_is_up_to_date() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/gen_baseline.py", "--check"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "docs/PHASE2_BASELINE.md is stale. If an agent, the indicators or the fixture "
        "changed deliberately, run `python scripts/gen_baseline.py` and commit the "
        f"result.\n{result.stderr}"
    )


def test_the_fixture_is_reproducible() -> None:
    """Regenerating the fixture must produce the identical file.

    The baseline counts are only meaningful if the input is stable.
    """
    fixture = REPO_ROOT / "aureon" / "data" / "fixtures" / "XAUUSD_M5.csv"
    before = fixture.read_bytes()
    subprocess.run(
        [sys.executable, "scripts/gen_fixtures.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    )
    assert fixture.read_bytes() == before, "gen_fixtures.py is not deterministic"

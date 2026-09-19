"""docs/CONTRACTS.md must match the models (cross-phase checklist).

The checklist requires `python scripts/gen_contracts.py` to produce no diff. This
test runs the generator's own `--check` so a model change that forgets to
regenerate the contract fails the suite rather than being noticed in Phase 8, when
the Vue types are generated from a stale document.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_contracts_are_up_to_date() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/gen_contracts.py", "--check"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "docs/CONTRACTS.md is stale. Run `python scripts/gen_contracts.py` "
        f"and commit the result.\n{result.stderr}"
    )


def test_generator_output_is_deterministic() -> None:
    """Two runs must be byte-identical, or the no-diff check is meaningless.

    This caught a real bug: rendering `Annotated[...]` included a lambda's memory
    address, and calling `default_factory` stamped a live timestamp -- both of
    which made the file differ on every single run.
    """
    contracts = REPO_ROOT / "docs" / "CONTRACTS.md"
    first = contracts.read_text(encoding="utf-8")
    subprocess.run(
        [sys.executable, "scripts/gen_contracts.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    )
    assert contracts.read_text(encoding="utf-8") == first

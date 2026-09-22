#!/usr/bin/env python3
"""Regenerate the Firestore ownership table inside ``docs/ARCHITECTURE.md`` (12, T-2).

    python scripts/gen_architecture.py            # rewrite the block
    python scripts/gen_architecture.py --check    # fail if it is stale

Only the block between the two markers is generated. Everything else in ARCHITECTURE.md is
prose written by hand, and this script must never touch it -- a generator that owned the whole
file would mean the document could only ever say what a generator can say.

The collection names come from ``paths.ALL_COLLECTIONS``, which is itself derived rather than
listed (11A, F-10). The writer/reader attribution is declared in ``aureon/storage/ownership.py``
and checked against that registry by ``tests/unit/test_ownership.py``, so a collection added to
``paths.py`` and forgotten here fails a test rather than quietly going undocumented.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from aureon.storage import ownership  # noqa: E402

DOC = REPO_ROOT / "docs" / "ARCHITECTURE.md"
BEGIN = "<!-- BEGIN GENERATED: ownership (python scripts/gen_architecture.py) -->"
END = "<!-- END GENERATED: ownership -->"


class MarkersMissing(RuntimeError):
    """``docs/ARCHITECTURE.md`` has no generated block to fill."""


def rendered() -> str:
    return f"{BEGIN}\n\n{ownership.render_table()}\n\n{END}"


def replaced(text: str) -> str:
    start = text.find(BEGIN)
    end = text.find(END)
    if start < 0 or end < 0 or end < start:
        raise MarkersMissing(
            f"{DOC} must contain the markers:\n  {BEGIN}\n  {END}"
        )
    return text[:start] + rendered() + text[end + len(END) :]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)

    if not DOC.exists():
        print(f"{DOC} does not exist", file=sys.stderr)
        return 1

    current = DOC.read_text(encoding="utf-8")
    try:
        wanted = replaced(current)
    except MarkersMissing as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.check:
        if wanted != current:
            print(
                f"{DOC} ownership table is stale; run `python scripts/gen_architecture.py` "
                "and commit the result.",
                file=sys.stderr,
            )
            return 1
        print(f"{DOC} ownership table is up to date.")
        return 0

    DOC.write_text(wanted, encoding="utf-8")
    print(f"wrote the ownership table into {DOC} ({len(ownership.OWNERSHIP)} collections)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

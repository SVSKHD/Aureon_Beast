#!/usr/bin/env python3
"""Rewrite the Evidence column of ``docs/PHASES.md`` from what is on disk (P-5).

    python scripts/update_phases.py            # rewrite the column
    python scripts/update_phases.py --check    # exit non-zero if it is stale

The phase table used to carry a Status column and nothing else, so "🟡 demo-account leg
outstanding" was a sentence somebody typed. Whether it was still true depended on nobody
forgetting to edit it -- and the direction it rots in is the dangerous one: a phase whose
evidence was produced, then invalidated, keeps its green mark.

So the Evidence cell is **derived**, on every run, from files that either exist or do not:

* a generated document (``docs/CONTRACTS.md``, ``docs/PHASE2_BASELINE.md``), optionally
  required to contain a named section, so a document that no longer reports the thing the
  gate asked for stops counting;
* a session evidence file under ``docs/evidence/``, which only
  ``scripts/session_run.py`` / ``scripts/session_verify.py`` produce;
* a drill evidence file, which only ``scripts/demo_drills.py --evidence`` produces.

A missing artefact renders as ``⬜ missing: …`` **with the command that would produce
it**. The
table can then be read as a claim about the repository rather than about somebody's memory.

## It never touches the Status column

Deliberately. Status is a judgement -- whether a partial pass counts, whether a leg is
outstanding -- and a script that inferred it from file existence would be making that
judgement silently. This tool answers one narrower question, and answers it honestly:
*what is there?*
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

PHASES = REPO_ROOT / "docs" / "PHASES.md"
EVIDENCE_HEADER = "Evidence"
PRESENT = "✅"
ABSENT = "⬜"


@dataclass(frozen=True)
class Artefact:
    """One thing that would count as evidence, and how to tell whether it is there."""

    label: str
    #: Repository-relative path, or a glob under docs/evidence for a dated artefact.
    path: str
    #: A heading or line the file must contain for the artefact to count.
    must_contain: str | None = None
    #: What to run when it is missing. The most useful half of a missing-evidence cell.
    produced_by: str | None = None
    #: Rendered without a link (a directory of guards rather than a document).
    link: bool = True

    def matches(self, root: Path) -> list[Path]:
        if any(ch in self.path for ch in "*?["):
            return sorted(root.glob(self.path))
        candidate = root / self.path
        return [candidate] if candidate.exists() else []

    def present(self, root: Path) -> tuple[bool, list[Path]]:
        found = [
            path
            for path in self.matches(root)
            if self.must_contain is None
            or self.must_contain in path.read_text(encoding="utf-8")
        ]
        return bool(found), found


@dataclass(frozen=True)
class PhaseEvidence:
    """The artefacts a phase's gate produces. Keyed as the table keys its rows."""

    #: The table's first cell: "0".."8", or "—" for the two unnumbered rows.
    phase: str
    #: For the "—" rows, a substring of the Scope cell that identifies which one.
    scope_contains: str | None
    artefacts: tuple[Artefact, ...]


CATALOGUE: tuple[PhaseEvidence, ...] = (
    PhaseEvidence(
        "0",
        None,
        (
            Artefact(
                label="boundary guards",
                path="tests/boundary/test_architecture_boundaries.py",
                link=False,
            ),
        ),
    ),
    PhaseEvidence(
        "1",
        None,
        (
            Artefact(
                label="CONTRACTS.md",
                path="docs/CONTRACTS.md",
                produced_by="python scripts/gen_contracts.py",
            ),
        ),
    ),
    PhaseEvidence(
        "2",
        None,
        (
            Artefact(
                label="PHASE2_BASELINE.md",
                path="docs/PHASE2_BASELINE.md",
                produced_by="python scripts/gen_baseline.py",
            ),
            Artefact(
                label="verified real session",
                path="evidence/session_*.md",
                must_contain="SESSION VERIFIED",
                produced_by="scripts/session_run.py, then scripts/session_verify.py",
            ),
        ),
    ),
    PhaseEvidence(
        "3",
        None,
        (
            Artefact(
                label="XAU_OUTCOME_V2 outcomes",
                path="docs/PHASE2_BASELINE.md",
                must_contain="## Detection outcomes — `XAU_OUTCOME_V2`",
                produced_by="python scripts/gen_baseline.py",
            ),
        ),
    ),
    PhaseEvidence(
        "4",
        None,
        (
            Artefact(
                label="drills on a demo account",
                path="evidence/demo_drills*.md",
                must_contain="| broker | `mt5` |",
                produced_by="scripts/demo_drills.py --all --broker mt5 --evidence …",
            ),
        ),
    ),
    PhaseEvidence(
        "5",
        None,
        (
            Artefact(
                label="drills on a demo account",
                path="evidence/demo_drills*.md",
                must_contain="| broker | `mt5` |",
                produced_by="scripts/demo_drills.py --all --broker mt5 --evidence …",
            ),
        ),
    ),
    PhaseEvidence(
        "6",
        None,
        (
            Artefact(
                label="live Discord session",
                path="evidence/discord_*.md",
                produced_by="the Phase 6 leg of docs/DEMO_EXECUTION_CHECKLIST.md",
            ),
        ),
    ),
    PhaseEvidence(
        "7",
        None,
        (
            Artefact(
                label="review reconciliation",
                path="docs/PHASE2_BASELINE.md",
                must_contain="Reconciled: every figure above agrees.",
                produced_by="python scripts/gen_baseline.py",
            ),
        ),
    ),
    PhaseEvidence("8", None, ()),
    PhaseEvidence(
        "9A",
        None,
        (
            Artefact(
                label="XAGUSD baseline",
                path="docs/PHASE2_BASELINE.md",
                must_contain="## Detection outcomes — `XAG_OUTCOME_V1`",
                produced_by="python scripts/gen_baseline.py",
            ),
            Artefact(
                label="verified XAGUSD session",
                path="evidence/session_*_XAGUSD.md",
                must_contain="SESSION VERIFIED",
                produced_by=(
                    "scripts/session_run.py --symbol XAGUSD, then "
                    "scripts/session_verify.py --symbol XAGUSD"
                ),
            ),
        ),
    ),
    PhaseEvidence(
        "9B",
        None,
        (
            Artefact(
                label="outcomes by volume/volatility context",
                path="docs/PHASE2_BASELINE.md",
                must_contain="### Outcomes by volume and volatility context — XAGUSD",
                produced_by="python scripts/gen_baseline.py",
            ),
        ),
    ),
    PhaseEvidence(
        "—",
        "Corrections slice",
        (
            Artefact(
                label="verified real session",
                path="evidence/session_*.md",
                must_contain="SESSION VERIFIED",
                produced_by="scripts/session_run.py, then scripts/session_verify.py",
            ),
        ),
    ),
    PhaseEvidence(
        "—",
        "Defect register",
        (
            Artefact(
                label="decisions 118–120",
                path="docs/PHASE1_DECISIONS.md",
                must_contain="| 120 |",
            ),
        ),
    ),
)


def render_cell(entry: PhaseEvidence, *, root: Path, docs: Path) -> str:
    """One Evidence cell: what is there, or what would produce what is not."""
    if not entry.artefacts:
        return "—"

    parts: list[str] = []
    for artefact in entry.artefacts:
        base = docs if artefact.path.startswith("evidence/") else root
        present, found = artefact.present(base)
        if not present:
            missing = f"{ABSENT} missing: {artefact.label}"
            if artefact.produced_by:
                missing += f" (`{artefact.produced_by}`)"
            parts.append(missing)
            continue
        if not artefact.link:
            # A file of guards rather than a document: report the COUNT, which is the
            # part that can regress without the file disappearing.
            count = len(
                re.findall(r"^def test_", found[0].read_text(encoding="utf-8"), re.M)
            )
            parts.append(f"{PRESENT} {count} {artefact.label}")
        elif len(found) == 1:
            # The label as the link text, not the filename: three phases are closed by
            # different sections of one generated document, and three identical links
            # would say nothing about which.
            parts.append(f"{PRESENT} [{artefact.label}]({_relative(found[0], docs)})")
        else:
            links = ", ".join(
                f"[{path.name}]({_relative(path, docs)})" for path in found[-3:]
            )
            parts.append(f"{PRESENT} {artefact.label}: {links}")
    return "<br>".join(parts)


def _relative(path: Path, docs: Path) -> str:
    """A link relative to docs/, since PHASES.md lives there."""
    return path.relative_to(docs).as_posix()


def _split_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _join_row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def rewrite(text: str, *, root: Path, docs: Path) -> str:
    """Rewrite the phase table's Evidence column, adding it if it is absent."""
    lines = text.splitlines()
    try:
        header = next(
            i
            for i, line in enumerate(lines)
            if line.startswith("| Phase |") and "Scope" in line
        )
    except StopIteration:
        raise ValueError("docs/PHASES.md has no phase table") from None

    columns = _split_row(lines[header])
    if EVIDENCE_HEADER not in columns:
        columns.append(EVIDENCE_HEADER)
        lines[header] = _join_row(columns)
        lines[header + 1] = _join_row(["---"] * len(columns))
    evidence_at = columns.index(EVIDENCE_HEADER)

    used: set[int] = set()
    for index in range(header + 2, len(lines)):
        line = lines[index]
        if not line.startswith("|"):
            break
        cells = _split_row(line)
        while len(cells) < len(columns):
            cells.append("")
        phase, scope = cells[0], cells[1]
        entry = next(
            (
                (i, e)
                for i, e in enumerate(CATALOGUE)
                if i not in used
                and e.phase == phase
                and (e.scope_contains is None or e.scope_contains in scope)
            ),
            None,
        )
        if entry is None:
            continue
        position, found = entry
        used.add(position)
        cells[evidence_at] = render_cell(found, root=root, docs=docs)
        lines[index] = _join_row(cells)

    missing = [
        f"{e.phase} {e.scope_contains or ''}".strip()
        for i, e in enumerate(CATALOGUE)
        if i not in used
    ]
    if missing:
        # A catalogue entry with no row means the table and this script disagree about
        # which phases exist, and the cell that did not get written would silently keep
        # whatever it said before.
        raise ValueError(
            "no row in docs/PHASES.md matched: " + ", ".join(missing)
        )
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="exit non-zero if stale")
    parser.add_argument("--path", type=Path, default=PHASES)
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    args = parser.parse_args(argv)

    current = args.path.read_text(encoding="utf-8")
    try:
        updated = rewrite(current, root=args.root, docs=args.root / "docs")
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2

    if args.check:
        if current != updated:
            print(
                f"{args.path} Evidence column is stale; run "
                "`python scripts/update_phases.py` and commit the result.",
                file=sys.stderr,
            )
            return 1
        print(f"{args.path} Evidence column is up to date.")
        return 0

    if current == updated:
        print(f"{args.path} is already up to date.")
        return 0
    args.path.write_text(updated, encoding="utf-8")
    print(f"updated {args.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

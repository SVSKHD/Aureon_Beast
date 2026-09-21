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


def _observed_symbols() -> tuple[str, ...]:
    """Every symbol the PROJECT supports, from the reviewed tuning table (11C, F-1).

    Deliberately not ``AureonConfig.from_env().symbols``, which was the first version and was
    wrong in a way worth recording: ``AUREON_SYMBOLS`` defaults to gold alone, so running this
    script on a box that had not exported it produced a Phase 2 gate demanding one verified
    session -- the evidence bar relaxing itself to match whoever happened to run the tool.
    An evidence gate is a claim about the project, so it reads a committed source.

    ``known_symbols()`` is that source: a symbol has a reviewed tuning entry only because
    somebody chose its thresholds in that instrument's own money (decision 141), which is
    exactly the population for which a gold session proves nothing about silver.
    """
    from aureon.config.symbol_tuning import known_symbols

    return known_symbols()


OBSERVED_SYMBOLS = _observed_symbols()
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
    #: When set, the artefact needs one matching file **per symbol**, and ``path`` carries
    #: a ``{symbol}`` placeholder (11C, F-1).
    #:
    #: The reason this exists: Phase 2's "verified real session" globbed
    #: ``evidence/session_*.md``, so ONE green file turned the column green -- and with two
    #: instruments observed, a verified gold session would have reported Phase 2 complete
    #: while silver had never been run at all. Thresholds are in points, and points are
    #: different money per instrument (decision 141); a parity claim about gold says nothing
    #: about silver. So every configured symbol has to have its own verified session, and
    #: the cell names the ones that do not.
    per_symbol: tuple[str, ...] = ()

    def matches(self, root: Path, path: str | None = None) -> list[Path]:
        pattern = path or self.path
        if any(ch in pattern for ch in "*?["):
            return sorted(root.glob(pattern))
        candidate = root / pattern
        return [candidate] if candidate.exists() else []

    def _qualifying(self, root: Path, path: str | None = None) -> list[Path]:
        return [
            found
            for found in self.matches(root, path)
            if self.must_contain is None
            or self.must_contain in found.read_text(encoding="utf-8")
        ]

    def missing_symbols(self, root: Path) -> tuple[str, ...]:
        """Which symbols have no qualifying file. Empty for a non-per-symbol artefact."""
        return tuple(
            symbol
            for symbol in self.per_symbol
            if not self._qualifying(root, self.path.format(symbol=symbol))
        )

    def present(self, root: Path) -> tuple[bool, list[Path]]:
        if self.per_symbol:
            if self.missing_symbols(root):
                # Deliberately no partial credit: a column that went green on one of two
                # instruments is the failure this field exists to prevent.
                return False, []
            found: list[Path] = []
            for symbol in self.per_symbol:
                found.extend(self._qualifying(root, self.path.format(symbol=symbol)))
            return True, found
        qualifying = self._qualifying(root)
        return bool(qualifying), qualifying


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
                path="evidence/session_*_{symbol}.md",
                must_contain="SESSION VERIFIED",
                produced_by=(
                    "scripts/session_run.py --symbol X, then "
                    "scripts/session_verify.py --symbol X, for each symbol"
                ),
                per_symbol=OBSERVED_SYMBOLS,
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
                label="outcomes by tick-volume/volatility context",
                path="docs/PHASE2_BASELINE.md",
                must_contain="### Outcomes by tick-volume and volatility context — XAGUSD",
                produced_by="python scripts/gen_baseline.py",
            ),
        ),
    ),
    PhaseEvidence(
        "11A",
        None,
        (
            Artefact(
                label="ops register",
                path="aureon/services/ops_events.py",
                must_contain="live_account_detected",
            ),
            Artefact(
                label="the frozen spec",
                path="docs/ARCHITECTURE.md",
                produced_by="supplied by the operator; see docs/DOCS_CHECK.md",
            ),
        ),
    ),
    PhaseEvidence(
        "11B",
        None,
        (
            Artefact(
                label="weekend cycle checks",
                path="tests/failure_injection/test_weekend_cycle.py",
                link=False,
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
            absent = artefact.missing_symbols(base)
            if absent:
                # Named, because "missing: verified real session" over two instruments does
                # not say which one still has to be run.
                missing += f" for {', '.join(absent)}"
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
    """A link relative to docs/, since PHASES.md lives there.

    An artefact OUTSIDE docs/ -- a module or a test file -- is linked from the repository
    root with ``../``, rather than crashing. The alternative, which the first 11A entry hit,
    is a tool that can only cite documents; and "the code that does this exists" is a
    perfectly good piece of evidence for a phase whose gate is a behaviour.
    """
    try:
        return path.relative_to(docs).as_posix()
    except ValueError:
        return "../" + path.relative_to(docs.parent).as_posix()


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

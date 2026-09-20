"""Every command the operator documents must exist (P-7).

A runbook is read at 02:00 by somebody who is not going to check whether the script it
names is still called that. A command that has been renamed, moved or deleted turns the
document from an instruction into a puzzle, and the failure is silent: nothing in the
repository notices, because prose is not compiled.

So this compiles it. Every ``python scripts/…`` / ``python main_….py`` and every
``make <target>`` mentioned in the operator documents is resolved against the repository,
and a `--flag` named in a runbook is resolved against the script's own parser.

Only **code** is scanned -- indented blocks and inline code spans -- and a make target has
to begin the fragment, because a checklist bullet's continuation line is indented exactly
like a code block and "only to make sure you know" is not a build step.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = REPO_ROOT / "Makefile"

#: The documents an operator is expected to follow. Deliberately a list rather than a
#: glob: a new operator document should be added here consciously, and a generated one
#: (docs/CONTRACTS.md, the evidence files) must not be.
OPERATOR_DOCS = (
    "docs/RUNBOOK.md",
    "docs/MT5_SESSION_CHECKLIST.md",
    "docs/DEMO_EXECUTION_CHECKLIST.md",
    "docs/evidence/README.md",
    "README.md",
)

SCRIPT = re.compile(r"\b((?:scripts/|main_)[\w./]*\.py)")
#: Anchored at the START of a code fragment, not searched within it. A checklist bullet's
#: continuation line is indented like a code block, so "only to make sure you know" would
#: otherwise be read as `make sure`. A command line begins with its command.
MAKE_TARGET = re.compile(r"^make ([a-z][\w-]*)")


def code_fragments(text: str) -> list[str]:
    """Indented code blocks and inline code spans, and nothing else."""
    fragments = [
        line.strip()
        for line in text.splitlines()
        if line.startswith("    ") and line.strip()
    ]
    fragments += re.findall(r"`([^`\n]+)`", text)
    return fragments


def documents() -> list[tuple[str, str]]:
    return [
        (name, (REPO_ROOT / name).read_text(encoding="utf-8"))
        for name in OPERATOR_DOCS
    ]


def make_targets() -> set[str]:
    return set(
        re.findall(r"^([a-z][\w-]*):", MAKEFILE.read_text(encoding="utf-8"), re.M)
    )


@pytest.mark.parametrize("name", OPERATOR_DOCS)
def test_the_document_exists(name: str) -> None:
    assert (REPO_ROOT / name).is_file(), f"{name} is referenced by the test roster"


def test_every_script_named_in_an_operator_document_exists() -> None:
    missing: list[str] = []
    seen = 0
    for name, text in documents():
        for fragment in code_fragments(text):
            for path in SCRIPT.findall(fragment):
                seen += 1
                if not (REPO_ROOT / path).is_file():
                    missing.append(f"{name}: {path}")
    assert seen > 15, "the roster stopped finding commands; the regex probably broke"
    assert not missing, "operator documents name scripts that do not exist:\n" + "\n".join(
        sorted(set(missing))
    )


def test_every_make_target_named_in_an_operator_document_exists() -> None:
    targets = make_targets()
    assert {"emulator", "test", "drills", "phases"} <= targets, targets

    missing: list[str] = []
    for name, text in documents():
        for fragment in code_fragments(text):
            for target in MAKE_TARGET.findall(fragment):
                if target not in targets:
                    missing.append(f"{name}: make {target}")
    assert not missing, "operator documents name make targets that do not exist:\n" + "\n".join(
        sorted(set(missing))
    )


#: Flags a document promises, and the script that must accept them. Written out rather
#: than scraped, because a flag in prose ("--force overrides it") is the case worth
#: checking and is not in a code block.
DOCUMENTED_FLAGS: dict[str, tuple[str, ...]] = {
    "scripts/preflight.py": ("--skip-mt5", "--json", "--drift-tolerance"),
    "scripts/session_run.py": ("--dry-run", "--force", "--market-date"),
    "scripts/session_verify.py": ("--no-write", "--archive-dir"),
    "scripts/demo_drills.py": (
        "--all",
        "--drill",
        "--list",
        "--broker",
        "--evidence",
        "--i-know-this-is-real-money",
    ),
    "scripts/report_outcomes.py": ("--rule", "--from", "--to", "--replay", "--markdown"),
    "scripts/update_phases.py": ("--check",),
    "scripts/compare_live_vs_replay.py": ("--symbol", "--archive-dir"),
}


@pytest.mark.parametrize(("script", "flags"), sorted(DOCUMENTED_FLAGS.items()))
def test_the_documented_flags_are_real(script: str, flags: tuple[str, ...]) -> None:
    """Resolved against the source, so a renamed flag fails here rather than in a shell."""
    source = (REPO_ROOT / script).read_text(encoding="utf-8")
    for flag in flags:
        assert f'"{flag}"' in source, f"{script} does not accept {flag}"


def test_the_runbook_covers_the_things_that_go_wrong() -> None:
    """Not a style rule: these are the situations an operator will actually be in, and a
    runbook that omits one sends them to the source at the worst possible moment."""
    runbook = (REPO_ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")
    for topic in (
        "Stop trading, now",
        "stuck in EXECUTING",
        "position Aureon does not know about",
        "observer died mid-session",
        "outbox is growing",
        "live-vs-replay reports a difference",
        "refuses to start on a symbol",
    ):
        assert topic in runbook, f"the runbook has no section about: {topic}"


def test_the_runbook_says_what_none_of_it_proves() -> None:
    """The most important section, and the easiest to quietly drop."""
    runbook = (REPO_ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")
    assert "## What none of it proves" in runbook
    assert "synthetic" in runbook
    assert "placeholders" in runbook

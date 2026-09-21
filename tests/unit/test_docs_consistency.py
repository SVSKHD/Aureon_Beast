"""Documentation that contradicts the code, caught mechanically (11A, F-11).

Docs rot silently. A stale sentence does not fail anything — it keeps telling a reader
something that stopped being true, and the reader has no way to know which sentences are
current. The three failure modes in this repo's history were all of that shape:

* ``CLAUDE.md`` told every contributor to read ``docs/ARCHITECTURE.md`` before any change, and
  that file has **never existed** in the repository. Ten package docstrings point at it too.
* ``docs/PHASES.md`` carried a caveat asking somebody to confirm that ``agent_version`` was
  excluded from ``detection_id`` — for four phases after the corrections slice put it back in.
* ``ALL_COLLECTIONS`` was hand-maintained and had drifted by two entries, so the contracts
  table simply did not mention two collections that were being written.

None of those is catchable by reading. All three are catchable by a test.

``docs/DOCS_CHECK.md`` holds the banned-phrase list and is meant to be edited by hand when a
correction lands; this file is the enforcement.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from aureon.storage import paths

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS = REPO_ROOT / "docs"
CHECK_FILE = DOCS / "DOCS_CHECK.md"

#: ``PHASE1_DECISIONS.md`` is append-only history: a decision records what was believed and why
#: at the time, and rewriting one to match the present would destroy the only record of the
#: change. ``DOCS_CHECK.md`` is the list itself, so it contains every phrase by construction.
EXEMPT = {"PHASE1_DECISIONS.md", "DOCS_CHECK.md"}


def docs_files() -> list[Path]:
    return sorted(
        p
        for p in DOCS.rglob("*.md")
        if p.name not in EXEMPT
    )


def banned_phrases() -> list[str]:
    """The phrase column of ``DOCS_CHECK.md``'s table.

    Parsed from the document rather than duplicated here, so the list a human maintains and the
    list the test enforces cannot drift apart — which would be this test committing the exact
    sin it exists to catch.
    """
    rows = []
    in_table = False
    for line in CHECK_FILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("| phrase "):
            in_table = True
            continue
        if in_table:
            if not line.startswith("|"):
                break
            if line.startswith("|---"):
                continue
            cell = line.split("|")[1].strip()
            # Strip the markdown code ticks the table uses for field names.
            rows.append(cell.strip("`"))
    return [row for row in rows if row]


# ── The register itself is wired up ───────────────────────────────────────────


def test_the_register_exists_and_has_phrases() -> None:
    """A parser that silently found nothing would make every check below vacuous."""
    assert CHECK_FILE.exists(), "docs/DOCS_CHECK.md is missing"
    phrases = banned_phrases()
    assert len(phrases) >= 5, f"parsed only {phrases} from the register's table"
    assert "is_demo" in phrases
    assert "exchange volume" in phrases


def test_there_are_docs_to_check() -> None:
    assert len(docs_files()) >= 5


# ── Every collection is documented ────────────────────────────────────────────


def documented_name(collection: str) -> str:
    """The name ``CONTRACTS.md`` renders for this collection.

    The contracts file deliberately shows the DEFAULT prefix rather than the live one, so it
    does not differ between a test run and production and can be checked in (decision 111).
    The test suite runs under ``aureon_test``, so comparing the runtime names directly would
    find nothing and report every collection as undocumented.
    """
    short = collection[len(f"{paths.PREFIX}_") :]
    return f"{paths.DEFAULT_COLLECTION_PREFIX}_{short}"


def test_every_collection_appears_in_the_contracts_file() -> None:
    """The registry is generated from ``paths.py`` (F-10), so a new collection makes the
    contracts file stale and this says which one."""
    contracts = (DOCS / "CONTRACTS.md").read_text(encoding="utf-8")
    missing = [
        name for name in paths.ALL_COLLECTIONS if documented_name(name) not in contracts
    ]
    assert not missing, (
        "these collections are in paths.ALL_COLLECTIONS and not in CONTRACTS.md — run "
        f"`python scripts/gen_contracts.py`: {missing}"
    )


def test_no_collection_is_documented_that_does_not_exist() -> None:
    """The other direction: a collection removed from the code and left in the docs.

    Looks only at the collections table, since prose elsewhere legitimately mentions a
    collection by its unprefixed name.
    """
    contracts = (DOCS / "CONTRACTS.md").read_text(encoding="utf-8")
    prefix = paths.DEFAULT_COLLECTION_PREFIX
    documented = set(re.findall(rf"\| `({prefix}_[a-z_]+)` \|", contracts))
    assert documented, "the collections table was not found in CONTRACTS.md"
    stale = documented - {documented_name(name) for name in paths.ALL_COLLECTIONS}
    assert not stale, f"documented but no longer defined in paths.py: {sorted(stale)}"


# ── Banned phrases ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("phrase", banned_phrases())
def test_no_document_contains_a_superseded_phrase(phrase: str) -> None:
    """One test per phrase, so a failure names the phrase rather than the whole list."""
    hits = []
    for path in docs_files():
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if phrase.lower() in line.lower():
                hits.append(f"{path.relative_to(REPO_ROOT)}:{number}")
    assert not hits, (
        f"superseded phrase {phrase!r} still appears — see docs/DOCS_CHECK.md for what to "
        f"say instead:\n  " + "\n  ".join(hits)
    )


# ── CLAUDE.md's reading list is real ──────────────────────────────────────────


@pytest.mark.xfail(
    strict=True,
    reason=(
        "docs/ARCHITECTURE.md has never been in the repository, and CLAUDE.md has told every "
        "contributor to read it since Phase 0. The frozen spec has to be SUPPLIED by the "
        "operator -- reconstructing it from memory would produce a plausible document that "
        "nobody wrote and everybody would then treat as authoritative, which is worse than "
        "the gap. strict=True so this turns RED the moment the file lands: that is the signal "
        "to delete this marker, not to keep it."
    ),
)
def test_every_file_the_standing_rules_tell_you_to_read_exists() -> None:
    """The one that would have caught ``docs/ARCHITECTURE.md``.

    CLAUDE.md has opened with "Read docs/ARCHITECTURE.md (frozen, §94), docs/CONTRACTS.md and
    docs/PHASE1_DECISIONS.md before any change" since Phase 0, and ARCHITECTURE.md has never
    been in the repository. Ten package docstrings point at it as well. An instruction to read
    a file that is not there is worse than no instruction: it reads as though the spec exists
    and somebody else has consulted it.
    """
    rules = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    referenced = set(re.findall(r"\bdocs/[A-Za-z0-9_]+\.md\b", rules))
    assert referenced, "CLAUDE.md names no docs at all; this test is checking nothing"
    missing = sorted(name for name in referenced if not (REPO_ROOT / name).exists())
    assert not missing, (
        "CLAUDE.md tells every contributor to read these before any change, and they are "
        f"not in the repository: {missing}"
    )

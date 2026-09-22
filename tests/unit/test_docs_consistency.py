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


def test_every_table_appears_in_the_contracts_file() -> None:
    """The PostgreSQL half of the same rule (13 S-2).

    ``docs/CONTRACTS.md``'s tables section is generated from ``Base.metadata``, so this
    cannot drift while the generator is run -- which is exactly the failure it guards
    against: a table added, the generator not run, and the checked-in contract silently
    describing a schema that no longer exists. ``python scripts/gen_contracts.py --check``
    is the other half and fails the same way.
    """
    from aureon.storage.postgres import tables as pg_tables  # noqa: F401  -- registers them
    from aureon.storage.postgres.models import Base

    contracts = (DOCS / "CONTRACTS.md").read_text(encoding="utf-8")
    missing = [name for name in sorted(Base.metadata.tables) if f"### `{name}`" not in contracts]
    assert not missing, (
        "these tables are in the models and not in CONTRACTS.md -- run "
        f"`python scripts/gen_contracts.py`: {missing}"
    )


def test_the_contracts_file_names_the_schema_revision() -> None:
    """So a reader can tell which schema the checked-in contract describes.

    A contracts file that did not say would be read as current whatever the code had moved
    on to, which is the drift this whole file exists to catch.
    """
    from aureon.storage.postgres.schema import EXPECTED_REVISION

    contracts = (DOCS / "CONTRACTS.md").read_text(encoding="utf-8")
    assert f"**{EXPECTED_REVISION}**" in contracts, (
        f"CONTRACTS.md does not name schema revision {EXPECTED_REVISION}"
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


def test_every_collection_appears_in_the_architecture_doc() -> None:
    """The ownership table has to cover the whole registry (12, T-2).

    ARCHITECTURE.md answers "which process writes ``trades``", and a collection missing from it
    answers that question with silence -- which reads exactly like "nothing writes it". The
    table is generated from ``aureon/storage/ownership.py``, so the remedy is one command.

    Written with ``{prefix}`` rather than a literal prefix, because the table must not change
    between a test run and production (the same reasoning as decision 111 for CONTRACTS.md).
    """
    doc = (DOCS / "ARCHITECTURE.md").read_text(encoding="utf-8")
    missing = [
        name
        for name in paths.ALL_COLLECTIONS
        if f"`{{prefix}}_{name[len(f'{paths.PREFIX}_'):]}`" not in doc
    ]
    assert not missing, (
        "these collections are in paths.ALL_COLLECTIONS and not in ARCHITECTURE.md — run "
        f"`python scripts/gen_architecture.py`: {missing}"
    )


def test_the_architecture_doc_states_what_it_is_not() -> None:
    """It must not be mistaken for the frozen spec the ``§`` references cite.

    The whole reason 11A refused to write this file was that a plausible document nobody wrote
    would be treated as authoritative. Writing it from the code answers that only as long as the
    document keeps saying which of the two it is; a later edit that dropped the disclaimer would
    quietly turn it into the thing 11A refused to produce.
    """
    doc = (DOCS / "ARCHITECTURE.md").read_text(encoding="utf-8")
    assert "as built" in doc.lower()
    assert "never been in this repository" in doc, (
        "ARCHITECTURE.md must keep saying that the frozen spec it does not contain has never "
        "been in the repository"
    )


def _invariants_section() -> str:
    """Just the numbered list under ``## The invariants``.

    Scoped rather than searched document-wide, which is what the first version of the test below
    did -- and a plant that removed ``assert_transition`` from the invariant itself survived,
    because the word still appeared in the Setups section three paragraphs later. A whole-document
    substring search cannot tell "this rule is listed" from "this word occurs somewhere".
    """
    doc = (DOCS / "ARCHITECTURE.md").read_text(encoding="utf-8")
    start = doc.find("## The invariants")
    assert start >= 0, "ARCHITECTURE.md has no `## The invariants` section"
    end = doc.find("\n## ", start + 1)
    section = doc[start : end if end > 0 else len(doc)]
    assert section.count("\n1. ") == 1, "the invariants section is not a numbered list"
    return section.lower()


def test_the_architecture_doc_lists_every_invariant_claude_md_states() -> None:
    """CLAUDE.md's non-negotiables and the doc's invariant list must not drift apart.

    Matched on a distinctive fragment of each rule rather than on whole sentences, because the
    two documents legitimately phrase them differently -- but a rule dropped from one of them is
    a rule the next contributor reads in only one place.
    """
    section = _invariants_section()
    for fragment in (
        "a detection never creates a trade",
        "assert_transition",
        "brokerinterface",
        "metatrader5",
        "tz-aware",
        "immutable",
        "aureon/storage",
        "tick data never goes to firestore",
        "tick_volume",
        "history_source",
        "never computes an indicator",
        "aureon/storage/paths.py",
    ):
        assert fragment in section, (
            f"the invariants section of ARCHITECTURE.md does not state the rule about "
            f"{fragment!r}"
        )


#: One required fragment per non-negotiable in ``CLAUDE.md``, in the same order. Declared rather
#: than derived from the bullet text: the first version of the test below picked "distinctive
#: tokens" out of each bullet by punctuation, which extracted the word "packages" from "reviews
#: packages." and failed on a rule the document states perfectly well. A heuristic whose failure
#: mode is a false alarm is worse than no test, because the next person deletes it.
#:
#: The count is asserted too, so a non-negotiable added to CLAUDE.md fails here until somebody
#: writes the invariant it corresponds to.
NON_NEGOTIABLE_FRAGMENTS: tuple[str, ...] = (
    "a detection never creates a trade",
    "human confirm",
    "assert_transition",
    "brokerinterface",
    "tz-aware",
    "immutable",
    "aureon/storage",
    "never computes an indicator",
)


def test_every_non_negotiable_in_claude_md_is_named_in_the_invariants() -> None:
    """A rule in CLAUDE.md and not in the document is a rule with no stated reason.

    CLAUDE.md is the file a contributor is told to read first and its non-negotiables are
    deliberately terse; the invariants section is where each one is explained and, where
    applicable, linked to the test that enforces it.
    """
    rules = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    start = rules.find("## Non-negotiables")
    assert start >= 0, "CLAUDE.md has no `## Non-negotiables` section"
    end = rules.find("\n## ", start + 1)
    bullets = [
        line.strip("- ").strip()
        for line in rules[start : end if end > 0 else len(rules)].splitlines()
        if line.startswith("- ")
    ]
    assert len(bullets) == len(NON_NEGOTIABLE_FRAGMENTS), (
        f"CLAUDE.md now has {len(bullets)} non-negotiables and this test knows "
        f"{len(NON_NEGOTIABLE_FRAGMENTS)}. Add the new rule to the invariants section of "
        "ARCHITECTURE.md and name its fragment here."
    )

    section = _invariants_section()
    for bullet, fragment in zip(bullets, NON_NEGOTIABLE_FRAGMENTS, strict=True):
        assert fragment in section, (
            f"the invariants section of ARCHITECTURE.md does not cover this CLAUDE.md rule: "
            f"{bullet[:100]!r} (expected to find {fragment!r})"
        )


def test_the_architecture_doc_describes_claim_before_post_the_way_the_code_does_it() -> None:
    """A doc that reversed this would describe a different system, and nothing would fail.

    The direction is a real tradeoff -- claiming first can LOSE a message, posting first can
    DUPLICATE one -- and the code claims first deliberately. A plant that reversed the doc's
    description survived every other test in this file, so the two are pinned to each other here.
    """
    doc = (DOCS / "ARCHITECTURE.md").read_text(encoding="utf-8")
    module = (
        REPO_ROOT / "aureon" / "storage" / "notification_repository.py"
    ).read_text(encoding="utf-8")

    assert "Claim before posting, not after" in module, (
        "the notification repository no longer documents its ordering; this test is pinning "
        "the document to nothing"
    )
    assert "claims first" in doc, (
        "ARCHITECTURE.md must say Aureon CLAIMS first. The code does "
        "(aureon/storage/notification_repository.py), and a document describing the opposite "
        "describes a system with a duplicate-alert failure mode instead of a lost-embed one."
    )
    # And it must keep saying what that costs, or it reads as a free win.
    assert "crash window" in doc.lower()


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


def test_every_file_the_standing_rules_tell_you_to_read_exists() -> None:
    """The one that would have caught ``docs/ARCHITECTURE.md``.

    It carried a ``xfail(strict=True)`` from 11A until 12 T-2, with the reason that the frozen
    spec had to be supplied rather than reconstructed from memory. T-2 resolved it the other
    way: the file now exists and is written **from the code**, which is a different thing from
    a reconstruction -- every claim in it is checkable against a named file, and the ownership
    table is generated. What it explicitly does NOT claim to be is the frozen spec, and it says
    so in its own first paragraph, because the ``§`` references throughout the codebase still
    point at a document the operator holds.

    The marker is gone rather than relaxed. A strict xfail that starts passing is a signal to
    delete it, which is exactly what it said it was.
    """
    rules = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    referenced = set(re.findall(r"\bdocs/[A-Za-z0-9_]+\.md\b", rules))
    assert referenced, "CLAUDE.md names no docs at all; this test is checking nothing"
    missing = sorted(name for name in referenced if not (REPO_ROOT / name).exists())
    assert not missing, (
        "CLAUDE.md tells every contributor to read these before any change, and they are "
        f"not in the repository: {missing}"
    )

"""The ownership table covers the registry, in both directions (12, T-2).

``aureon/storage/ownership.py`` is the one hand-maintained list left in the storage layer, and
11A's F-10 is the reason to be suspicious of it: the previous hand-maintained list
(``ALL_COLLECTIONS``) had drifted by two entries within one phase, and nothing failed -- the two
collections were simply not considered by anything that read the registry.

So the drift is made to fail here. The collection NAMES come from ``paths.py``, derived from its
own namespace; the writer/reader attribution cannot be derived that way and is declared. These
tests assert the two sets are the same set.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from aureon.storage import ownership, paths

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_every_registered_collection_has_an_owner() -> None:
    missing = sorted(ownership.registered() - ownership.declared())
    assert not missing, (
        "these collections exist in paths.py with no entry in aureon/storage/ownership.py, so "
        f"nothing documents who writes them: {missing}"
    )


def test_no_owner_is_declared_for_a_collection_that_does_not_exist() -> None:
    stale = sorted(ownership.declared() - ownership.registered())
    assert not stale, (
        f"declared in ownership.py but no longer defined in paths.py: {stale}"
    )


def test_the_registry_is_not_empty() -> None:
    """A parser or a namespace scan that found nothing would make both tests above vacuous."""
    assert len(ownership.registered()) >= 15
    assert len(ownership.OWNERSHIP) == len(ownership.registered())


@pytest.mark.parametrize("row", ownership.OWNERSHIP, ids=lambda r: r.name)
def test_every_collection_has_at_least_one_writer_and_one_reader(row) -> None:
    """A collection nobody reads is dead weight; one nobody writes is a bug or a leftover."""
    assert row.writers, f"{row.name} has no writer"
    assert row.readers, f"{row.name} has no reader"
    assert row.what.strip(), f"{row.name} has no description"


@pytest.mark.parametrize("row", ownership.OWNERSHIP, ids=lambda r: r.name)
def test_every_named_process_is_a_real_process(row) -> None:
    """Catches a typo, and a process name invented for one row.

    Six names and no more: five entrypoints plus ``tools`` for the scripts a human runs. A
    seventh would mean either a new service nobody documented or a misspelling of one of these,
    and both read the same in a rendered table.
    """
    for name in (*row.writers, *row.readers):
        assert name in ownership.PROCESSES, (
            f"{row.name} names {name!r}, which is not one of {ownership.PROCESSES}"
        )


def test_every_row_is_keyed_by_a_paths_constant_not_a_string() -> None:
    """§83: a bare collection literal must not exist outside ``paths.py``.

    The first draft of this table held twenty of them and the boundary test caught it. Asserted
    here as well as there, because the remedy that would have hidden it -- exempting this file --
    is the one somebody reaches for when the boundary test fails.
    """
    for row in ownership.OWNERSHIP:
        assert row.collection in paths.ALL_COLLECTIONS, (
            f"{row.collection!r} is not a collection paths.py defines; the table must be keyed "
            "by the constant, e.g. paths.TRADES"
        )
        assert row.collection == f"{paths.PREFIX}_{row.name}"

    source = (REPO_ROOT / "aureon" / "storage" / "ownership.py").read_text(encoding="utf-8")
    bare = [
        f"{row.name!r}"
        for row in ownership.OWNERSHIP
        if f'"{row.name}"' in source or f"'{row.name}'" in source
    ]
    assert not bare, f"ownership.py holds bare collection literals: {bare}"


def test_the_table_renders_with_the_placeholder_prefix_not_the_live_one() -> None:
    """So the checked-in document does not differ between a test run and production.

    The suite runs under ``aureon_test``; a table rendered with the live prefix would be
    rewritten on every developer's machine and the diff would be noise (decision 111).
    """
    table = ownership.render_table()
    assert f"{{prefix}}_{ownership.unprefixed(paths.DETECTIONS)}" in table
    assert paths.PREFIX not in table.replace("{prefix}", "")


def test_the_rendered_table_is_sorted() -> None:
    """A generated block whose order depends on a dict is a diff nobody can read."""
    table = ownership.render_table()
    names = [
        line.split("`")[1].removeprefix("{prefix}_")
        for line in table.splitlines()
        if line.startswith("| `{prefix}_")
    ]
    assert names == sorted(names)
    assert len(names) == len(ownership.OWNERSHIP)


# ── the two attributions that are ENFORCED and not merely declared ────────────


def test_discord_is_declared_as_a_writer_of_only_what_it_may_write() -> None:
    """The declared table and §71's permission list have to agree.

    This is a cross-check on the table, not the enforcement -- ``tests/boundary`` enforces the
    real rule. But a table claiming Discord writes ``detections`` would be a documented lie
    about the one boundary that matters most to a reader, so it fails here.
    """
    # Built from the constants, not from names typed here: §83 forbids a bare collection
    # literal outside paths.py, and the boundary test scans tests/ as well -- deliberately,
    # because a stale literal in a test helper matches nothing and makes the assertion pass
    # vacuously.
    allowed = {
        ownership.unprefixed(name)
        for name in (
            paths.TRADE_REQUESTS,
            paths.CONTROL_REQUESTS,
            paths.SETTINGS,
            paths.ALERTS,
            paths.TRADE_NOTES,
            paths.ASSESSMENTS,
            paths.NOTIFICATIONS,
            paths.AUDIT_LOGS,
            paths.HEARTBEATS,
        )
    }
    writes = {row.name for row in ownership.OWNERSHIP if ownership.DISCORD in row.writers}
    assert writes <= allowed, (
        "ownership.py says Discord writes collections §71 does not permit it: "
        f"{sorted(writes - allowed)}"
    )


def test_only_the_monitor_is_declared_as_a_writer_of_trades() -> None:
    """MT5 is the truth about what a trade did; one process records it.

    Two writers would mean two answers to "did this close", and the losing one would look just
    as authoritative in Firestore.
    """
    assert ownership.BY_COLLECTION[paths.TRADES].writers == (ownership.MONITOR,)


def test_only_the_observer_is_declared_as_a_writer_of_detections() -> None:
    """Detections are immutable and their ids are deterministic; a second writer breaks both."""
    assert ownership.BY_COLLECTION[paths.DETECTIONS].writers == (ownership.OBSERVER,)
    assert ownership.BY_COLLECTION[paths.DETECTION_EVALUATIONS].writers == (
        ownership.OBSERVER,
    )


def test_the_executor_writes_nothing_the_observer_owns() -> None:
    """The executor cannot decide, which starts with it not being able to record a decision."""
    observation = (
        paths.DETECTIONS,
        paths.DETECTION_EVALUATIONS,
        paths.SESSIONS,
        paths.SYSTEM_STATE,
    )
    for name in observation:
        assert ownership.EXECUTOR not in ownership.BY_COLLECTION[name].writers, (
            f"the executor is declared as a writer of {name}"
        )


# ── the declaration is at least consistent with the code ──────────────────────


def _repositories_for(collection: str) -> set[str]:
    """Storage modules that reference this collection's path helper or constant.

    Parsed rather than grepped, so a mention inside a docstring does not count. Not a proof of
    ownership -- a repository is used by whoever constructs it -- but it does catch a collection
    declared here that no repository in the storage layer touches at all.
    """
    # Most collections are addressed through ``paths.NAME`` or ``paths.name_path``; several have
    # a singular or otherwise irregular helper instead. The map is keyed by the paths CONSTANT
    # rather than by a name typed here, for the reason in the §83 test above.
    irregular = {
        paths.DETECTION_EVALUATIONS: "detection_evaluation_path",
        paths.HEARTBEATS: "heartbeat_path",
        paths.SYMBOL_SPECS: "symbol_spec_path",
        paths.MARKET_DAYS: "market_day_path",
        paths.MARKET_DAY_FRAMES: "market_day_frame_path",
        paths.AUDIT_LOGS: "audit_path",
        paths.DAILY_REVIEWS: "daily_review_path",
        paths.WEEKLY_REVIEWS: "weekly_review_path",
        paths.SETTINGS: "execution_settings_path",
        paths.TRADE_NOTES: "trade_note_path",
        paths.OPS_EVENTS: "ops_event_path",
        paths.NOTIFICATIONS: "notification_path",
    }
    wanted = {collection.upper(), f"{collection}_path"}
    helper = irregular.get(paths.collection(collection))
    if helper:
        wanted.add(helper)

    found: set[str] = set()
    storage = REPO_ROOT / "aureon" / "storage"
    for path in sorted(storage.glob("*.py")):
        if path.name in {"paths.py", "ownership.py", "__init__.py"}:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in wanted:
                found.add(path.name)
    return found


@pytest.mark.parametrize("row", ownership.OWNERSHIP, ids=lambda r: r.name)
def test_every_declared_collection_is_reachable_through_a_repository(row) -> None:
    """Firestore writes go through ``aureon/storage`` only (CLAUDE.md).

    So every collection must be addressed by at least one repository there. A collection with no
    repository is either dead or being written by a raw client call somewhere it should not be.
    """
    modules = _repositories_for(row.name)
    assert modules, (
        f"no module in aureon/storage addresses {row.name}; either it is dead or "
        "something is reaching Firestore without a repository"
    )

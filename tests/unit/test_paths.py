"""Firestore paths (decisions 9, 10, 11)."""

from __future__ import annotations

import pytest

from aureon.storage import paths


def test_there_is_no_pending_orders_collection() -> None:
    """Decision 9: a pending order IS the PENDING trade request.

    A second collection would be a second source of truth about the same order,
    and the two would eventually disagree.
    """
    assert "pending_orders" not in paths.ALL_COLLECTIONS


def test_heartbeats_are_per_service_documents() -> None:
    """Decision 10: heartbeats/{service} is the source of truth."""
    assert paths.heartbeat_path("observer") == f"{paths.HEARTBEATS}/observer"
    assert set(paths.SERVICES) == {"observer", "executor", "monitor", "discord"}


def test_execution_settings_live_at_settings_execution() -> None:
    """Decision 11: the runtime override lives at a fixed, single path."""
    assert paths.execution_settings_path() == f"{paths.SETTINGS}/execution"


def test_system_state_is_one_document_per_symbol() -> None:
    """Per symbol since 9A. A shared document means gold's candle close resets the write
    throttle and suppresses silver's state for the next few seconds."""
    from aureon.models.enums import Timeframe

    assert (
        paths.system_state_path("XAUUSD", Timeframe.M5)
        == f"{paths.SYSTEM_STATE}/XAUUSD_M5"
    )
    assert paths.system_state_path("XAGUSD", "M5") == f"{paths.SYSTEM_STATE}/XAGUSD_M5"
    assert paths.system_state_doc_id("XAGUSD", Timeframe.M5) == "XAGUSD_M5"


def test_a_symbol_or_timeframe_must_be_named() -> None:
    """There is no "current" document any more, so there is no default to fall back to."""
    for bad in (("", "M5"), ("XAUUSD", "")):
        try:
            paths.system_state_path(*bad)
        except ValueError:
            continue
        raise AssertionError(f"expected a ValueError for {bad}")


def test_evaluation_path_pairs_detection_with_rule() -> None:
    assert (
        paths.detection_evaluation_path("d1", "EMA_OUTCOME_V1")
        == f"{paths.DETECTION_EVALUATIONS}/d1__EMA_OUTCOME_V1"
    )


def test_reviews_are_keyed_so_a_regenerated_period_overwrites() -> None:
    """Phase 7 requires re-running a period to be idempotent."""
    assert paths.daily_review_path("2026-09-18") == f"{paths.DAILY_REVIEWS}/2026-09-18"
    assert paths.weekly_review_path(2026, 38) == f"{paths.WEEKLY_REVIEWS}/2026-W38"
    assert paths.weekly_review_path(2026, 3) == f"{paths.WEEKLY_REVIEWS}/2026-W03"


@pytest.mark.parametrize("bad", ["", "a/b"])
def test_empty_or_nested_ids_are_rejected(bad: str) -> None:
    """An empty id makes Firestore auto-generate one, silently duplicating a
    document whose whole purpose is to be idempotent."""
    with pytest.raises(ValueError):
        paths.detection_path(bad)


def test_every_collection_is_listed() -> None:
    assert len(paths.ALL_COLLECTIONS) == len(set(paths.ALL_COLLECTIONS))
    assert paths.DETECTIONS in paths.ALL_COLLECTIONS


# ── The collection prefix (§83, decision 111) ─────────────────────────────────


def test_every_path_carries_the_prefix() -> None:
    """One variable must move a whole deployment's data.

    Asserted against the resolved prefix rather than a literal, because the tests run
    under ``aureon_test`` and production runs under ``aureon_beast`` -- pinning either
    string would make this test pass only in one of the two places it matters.
    """
    builders = [
        paths.detection_path("d1"),
        paths.detection_evaluation_path("d1", "R1"),
        paths.session_path("2026-09-18", "london"),
        paths.trade_request_path("r1"),
        paths.trade_path("t1"),
        paths.control_request_path("c1"),
        paths.audit_path("a1"),
        paths.heartbeat_path("observer"),
        paths.system_state_path("XAUUSD", "M5"),
        paths.execution_settings_path(),
        paths.symbol_spec_path("XAUUSD"),
        paths.daily_review_path("2026-09-18"),
        paths.weekly_review_path(2026, 38),
    ]
    for path in builders:
        assert path.startswith(f"{paths.PREFIX}_"), f"{path} is not prefixed"


def test_the_prefix_is_configurable() -> None:
    """Checked through ``collection()`` because reloading the module mid-suite would
    leave every already-imported constant pointing at the old prefix."""
    assert paths.collection("trades") == f"{paths.PREFIX}_trades"


def test_a_collection_name_cannot_forge_a_nested_path() -> None:
    import pytest

    with pytest.raises(ValueError, match="'/'"):
        paths.collection("trades/evil")
    with pytest.raises(ValueError, match="empty"):
        paths.collection("")


def test_an_empty_prefix_falls_back_to_the_default(monkeypatch) -> None:
    """An unset-but-present variable must not produce ``_detections``.

    A leading-underscore collection would be a second, empty collection that reads as
    "no data yet" -- the same silent failure the prefix exists to make impossible.
    """
    monkeypatch.setenv("AUREON_COLLECTION_PREFIX", "")
    assert paths.collection_prefix() == paths.DEFAULT_COLLECTION_PREFIX
    monkeypatch.setenv("AUREON_COLLECTION_PREFIX", "___")
    assert paths.collection_prefix() == paths.DEFAULT_COLLECTION_PREFIX


# ── 11A F-10: the registry cannot drift ───────────────────────────────────────


def _collection_names_from_source() -> set[str]:
    """Every ``X = collection("name")`` in ``paths.py``, read from the SOURCE.

    An INDEPENDENT derivation, and that is the whole point. ``ALL_COLLECTIONS`` is generated
    by scanning the module's runtime namespace, so a test that scanned the same namespace
    would be asking the implementation to agree with itself. Parsing the text catches the one
    failure mode the runtime scan cannot see: a constant defined BELOW the line where
    ``ALL_COLLECTIONS`` is assigned, which the scan would miss and nothing would report.
    """
    import ast
    from pathlib import Path

    source = Path(paths.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        call = node.value
        if not isinstance(call, ast.Call):
            continue
        callee = call.func
        if not (isinstance(callee, ast.Name) and callee.id == "collection"):
            continue
        if not call.args or not isinstance(call.args[0], ast.Constant):
            continue
        found.add(f"{paths.PREFIX}_{call.args[0].value}")
    return found


def test_every_collection_constant_is_in_the_registry() -> None:
    """The flaw this closes had already happened twice in one phase.

    ``assessments`` and ``trade_notes`` were added in 9D and never added to the
    hand-maintained tuple, so every consumer of the registry — the contracts table, the
    emulator cleanup, the docs check — silently did not know two collections existed. Nothing
    failed; the collections simply were not considered.
    """
    from_source = _collection_names_from_source()
    assert from_source, "the source scan found nothing; the parser is broken, not the module"
    assert set(paths.ALL_COLLECTIONS) == from_source, (
        "ALL_COLLECTIONS and the collection() calls in paths.py disagree:\n"
        f"  missing from the registry: {sorted(from_source - set(paths.ALL_COLLECTIONS))}\n"
        f"  in the registry but not defined: "
        f"{sorted(set(paths.ALL_COLLECTIONS) - from_source)}"
    )


def test_the_registry_holds_no_document_ids() -> None:
    """``LEGACY_SYSTEM_STATE_DOC`` and friends are upper-case strings too.

    The scan discriminates on the PREFIX rather than on naming convention, precisely so a
    doc-id constant cannot be swept into a list of collections — a cleanup routine iterating
    that list would then try to delete a collection named ``current``.
    """
    for value in paths.ALL_COLLECTIONS:
        assert value.startswith(f"{paths.PREFIX}_")
    assert paths.LEGACY_SYSTEM_STATE_DOC not in paths.ALL_COLLECTIONS
    assert paths.EXECUTION_SETTINGS_DOC not in paths.ALL_COLLECTIONS
    assert paths.PREFIX not in paths.ALL_COLLECTIONS


def test_the_registry_is_sorted_and_unique() -> None:
    """Stable ordering, so the contracts file does not differ between runs."""
    assert list(paths.ALL_COLLECTIONS) == sorted(paths.ALL_COLLECTIONS)
    assert len(set(paths.ALL_COLLECTIONS)) == len(paths.ALL_COLLECTIONS)


def test_the_9d_collections_are_present() -> None:
    """Named explicitly, because these two are the ones that were missing."""
    assert paths.ASSESSMENTS in paths.ALL_COLLECTIONS
    assert paths.TRADE_NOTES in paths.ALL_COLLECTIONS

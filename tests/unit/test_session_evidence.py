"""The session record and the checks that fill its second half (P-3).

Two things are being protected here, and they are different kinds of thing.

The **document** must not be able to lie about the past. Its opening half is written
before the observer starts and a verification re-run must preserve it byte for byte, so
that a session that went wrong cannot be retroactively improved into one that did not.
That is a textual property, and it is tested textually.

The **checks** must not be able to pass on an absence. Each one exists because a specific
failure is silent: an observer that died at lunchtime leaves an archive that looks normal
and simply stops, a session whose outbox never drained leaves stored counts that are a
lower bound, a missing archive leaves nothing to compare at all. Each is tested against a
real parquet archive and a real repository write into an in-memory client, because a check
mocked at the level it is meant to verify verifies nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aureon.config.config import AureonConfig
from aureon.models.enums import Timeframe
from aureon.services.checks import CheckReport, CheckResult, Status
from aureon.services.session_evidence import (
    AFTER_MARKER,
    NOT_YET_VERIFIED,
    Block,
    SessionDocument,
    SessionMeta,
    evidence_path,
    git_commit,
    git_dirty,
    orphan_document,
    render_closing,
    replace_after,
    split_at_marker,
)
from aureon.services.session_verifier import (
    HEARTBEAT_TOLERANCE_CANDLES,
    Comparison,
    SessionVerifier,
)
from aureon.storage import paths
from tests.conftest import MARKET_TZ, InMemoryFirestore

REPO_ROOT = Path(__file__).resolve().parents[2]
MARKET_DATE = "2026-09-16"
NOW = datetime(2026, 9, 16, 21, 5, tzinfo=UTC)


def meta(**overrides) -> SessionMeta:
    base = {
        "market_date": MARKET_DATE,
        "symbol": "XAUUSD",
        "timeframe": "M5",
        "market_tz": MARKET_TZ,
        "account_scope": "primary",
        "collection_prefix": paths.PREFIX,
        "evaluation_rule_id": "XAU_OUTCOME_V2",
        "ema_fast": 20,
        "ema_slow": 50,
        "commit": "a" * 40,
        "dirty": False,
        "terminal": "connected to MetaTrader 5 build 4410",
        "server": "BrokerX-Demo",
        "started_at": datetime(2026, 9, 15, 23, 0, tzinfo=UTC),
    }
    base.update(overrides)
    return SessionMeta(**base)


# ── The document ─────────────────────────────────────────────────────────────


def test_the_evidence_file_is_named_on_the_broker_date() -> None:
    path = evidence_path(MARKET_DATE, root=Path("docs/evidence"))
    assert path == Path("docs/evidence/session_2026-09-16.md")


def test_the_opening_half_carries_what_nobody_can_reconstruct_later(tmp_path) -> None:
    document = SessionDocument(
        meta=meta(),
        blocks=[Block(title="Preflight", body="PASS config ...", command="x")],
    )
    text = document.render()

    for fragment in (
        "# Session evidence — XAUUSD M5 2026-09-16",
        "| commit | `aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa` |",
        "build 4410",
        "BrokerX-Demo",
        f"| collection prefix | `{paths.PREFIX}` |",
        "| EMA pair | 20 / 50 |",
        "### Preflight",
        "    $ x",
        AFTER_MARKER,
        NOT_YET_VERIFIED,
    ):
        assert fragment in text

    path = document.write(tmp_path / "evidence" / "session.md")
    assert path.read_text(encoding="utf-8") == text


def test_an_unstopped_session_says_unknown_rather_than_nothing() -> None:
    rows = dict(meta(ended_at=None).rows())
    assert rows["observer stopped"] == "—"


def test_a_dirty_tree_is_recorded_beside_the_commit() -> None:
    """A session run from a dirty tree is evidence about code that exists nowhere else."""
    assert "uncommitted changes" in dict(meta(dirty=True).rows())["commit"]
    assert "tree state unknown" in dict(meta(dirty=None).rows())["commit"]


def test_a_missing_commit_is_a_visible_gap_not_a_guess() -> None:
    assert dict(meta(commit=None).rows())["commit"] == "—"


def test_the_commit_helpers_answer_in_this_repository() -> None:
    commit = git_commit(cwd=REPO_ROOT)
    assert commit is not None and len(commit) == 40
    assert git_dirty(cwd=REPO_ROOT) in (True, False)


def test_the_commit_helpers_return_none_outside_a_repository(tmp_path) -> None:
    """``None`` rather than a crash: the tool must still write a record."""
    assert git_commit(cwd=tmp_path) is None or git_dirty(cwd=tmp_path) is not None


# ── The before/after split ───────────────────────────────────────────────────


def _report() -> CheckReport:
    return CheckReport(
        results=[
            CheckResult("archive", Status.PASS, "288 candles"),
            CheckResult("live_vs_replay", Status.FAIL, "differences", remedy="look here"),
        ]
    )


def test_verification_replaces_the_second_half_and_nothing_above_it() -> None:
    original = SessionDocument(meta=meta()).render()
    head, _tail = split_at_marker(original)

    once = replace_after(original, render_closing(_report(), []))
    twice = replace_after(once, render_closing(_report(), [], verified_at=NOW))

    assert once.startswith(head), "the pre-session half must survive byte for byte"
    assert twice.startswith(head), "and must survive a second verification too"
    assert NOT_YET_VERIFIED not in once
    assert "SESSION NOT VERIFIED" in once
    assert f"Verified at `{NOW.isoformat()}`" in twice


def test_a_file_without_the_marker_is_refused() -> None:
    """Rather than appending to a document whose provenance is unknown."""
    with pytest.raises(ValueError, match="no '## After the close' line"):
        replace_after("# something a human wrote\n", render_closing(_report(), []))


def test_the_header_explaining_the_marker_is_not_mistaken_for_it() -> None:
    """A real bug, found by a test: the header names the marker, so a substring split
    finds that sentence first and truncates the document at it -- while
    ``after.startswith(head)`` still passes, because the head was cut in the same place.
    """
    original = SessionDocument(meta=meta()).render()
    assert f"`{AFTER_MARKER}`" in original, "the header quotes the marker"
    head, tail = split_at_marker(original)
    assert tail.startswith(AFTER_MARKER)
    assert f"`{AFTER_MARKER}`" in head, "the quoted mention stays in the opening half"
    assert "| market date |" in head, "which means the metadata table survives"

    rewritten = replace_after(original, render_closing(_report(), []))
    assert "| market date |" in rewritten
    assert rewritten.count(AFTER_MARKER) == original.count(AFTER_MARKER)


def test_the_closing_half_carries_the_table_the_remedies_and_the_blocks() -> None:
    closing = render_closing(
        _report(),
        [Block(title="Live vs replay", body="DIFFERENCES FOUND", command="compare")],
    )
    assert "| status | check | detail |" in closing
    assert "| FAIL | `live_vs_replay` | differences |" in closing
    assert "- `live_vs_replay`: look here" in closing
    assert "### Live vs replay" in closing
    assert "DIFFERENCES FOUND" in closing


def test_a_pipe_in_a_broker_error_does_not_break_the_table() -> None:
    report = CheckReport(
        results=[CheckResult("archive", Status.FAIL, "bad | worse")]
    )
    assert "bad \\| worse" in "\n".join(report.markdown())


def test_an_unbracketed_session_says_so_in_the_document() -> None:
    document = orphan_document(meta(started_at=None))
    text = document.render()
    assert "No pre-session record" in text
    assert "which is not necessarily the one the observer ran at" in text


# ── The checks ───────────────────────────────────────────────────────────────


@pytest.fixture
def day_candles(candles):
    """One broker day of fixture candles, contiguous."""
    day = [c for c in candles if c.open_time.market_date == MARKET_DATE]
    assert len(day) > 100, "the fixture must cover this broker date"
    return day


def write_archive(root: Path, candles) -> Path:
    from aureon.data.live_candle_archive import LiveCandleArchive

    archive = LiveCandleArchive(root)
    for candle in candles:
        archive.add(candle)
    archive.flush_all()
    return root


def verifier(
    tmp_path: Path,
    *,
    client=None,
    comparison: Comparison | None = None,
    **kwargs,
) -> SessionVerifier:
    return SessionVerifier(
        AureonConfig(),
        market_date=MARKET_DATE,
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        archive_dir=kwargs.pop("archive_dir", tmp_path / "archive"),
        outbox_path=kwargs.pop("outbox_path", tmp_path / "outbox.db"),
        client_factory=(lambda: client if client is not None else InMemoryFirestore()),
        comparison_runner=(
            None if comparison is None else (lambda: comparison)
        ),
        now=lambda: NOW,
        **kwargs,
    )


def test_a_present_archive_reports_its_span(tmp_path, day_candles) -> None:
    write_archive(tmp_path / "archive", day_candles)
    result = verifier(tmp_path).check_archive()
    assert result.status is Status.PASS
    assert f"{len(day_candles)} candles" in result.detail
    assert "h)" in result.detail


def test_a_missing_archive_fails_and_says_why_it_matters(tmp_path) -> None:
    result = verifier(tmp_path).check_archive()
    assert result.status is Status.FAIL
    assert "does not exist" in result.detail
    assert "SIGTERM" in (result.remedy or "")


def test_a_contiguous_archive_has_no_gaps(tmp_path, day_candles) -> None:
    write_archive(tmp_path / "archive", day_candles)
    check = verifier(tmp_path)
    check.check_archive()
    assert check.check_archive_gaps().status is Status.PASS


def test_a_hole_in_the_middle_of_the_day_is_reported(tmp_path, day_candles) -> None:
    """An hour missing mid-session still produces a full-looking outcome table."""
    with_hole = day_candles[:40] + day_candles[52:]
    write_archive(tmp_path / "archive", with_hole)
    check = verifier(tmp_path)
    check.check_archive()
    result = check.check_archive_gaps()
    assert result.status is Status.WARN
    assert "1 gap(s)" in result.detail
    assert "60 min" in result.detail
    assert "INVALID" in (result.remedy or "")
    assert any(b.title == "Gaps in the archive" for b in check.blocks)


def test_gaps_are_not_checked_without_an_archive(tmp_path) -> None:
    check = verifier(tmp_path)
    check.check_archive()
    assert check.check_archive_gaps().status is Status.SKIP


def test_an_identical_comparison_passes(tmp_path, day_candles) -> None:
    write_archive(tmp_path / "archive", day_candles)
    check = verifier(tmp_path, comparison=Comparison(0, "IDENTICAL"))
    check.check_archive()
    result = check.check_live_vs_replay()
    assert result.status is Status.PASS
    assert any(b.title == "Live vs replay" for b in check.blocks)


def test_a_differing_comparison_fails_and_keeps_its_output(tmp_path, day_candles) -> None:
    write_archive(tmp_path / "archive", day_candles)
    check = verifier(tmp_path, comparison=Comparison(1, "DIFFERENCES FOUND\n  missing 3"))
    check.check_archive()
    result = check.check_live_vs_replay()
    assert result.status is Status.FAIL
    assert "missing 3" in check.blocks[-1].body


def test_a_comparison_that_could_not_run_is_not_a_pass(tmp_path, day_candles) -> None:
    write_archive(tmp_path / "archive", day_candles)
    check = verifier(tmp_path, comparison=Comparison(2, "no such file"))
    check.check_archive()
    result = check.check_live_vs_replay()
    assert result.status is Status.FAIL
    assert "could not run" in result.detail


def test_nothing_is_compared_without_an_archive(tmp_path) -> None:
    check = verifier(tmp_path, comparison=Comparison(0, "IDENTICAL"))
    check.check_archive()
    result = check.check_live_vs_replay()
    assert result.status is Status.SKIP
    assert "Nothing was compared" in (result.remedy or "")


# ── Stored detections ────────────────────────────────────────────────────────


@pytest.fixture
def stored(day_candles):
    """An in-memory Firestore holding one broker day's detections and evaluations."""
    from aureon.evaluation.backfill import run_backfill
    from aureon.evaluation.rules import XAU_OUTCOME_V2
    from aureon.storage.detection_repository import DetectionRepository
    from aureon.storage.evaluation_repository import EvaluationRepository
    from tests.conftest import cross_agent

    result = run_backfill(
        day_candles,
        [cross_agent()],
        XAU_OUTCOME_V2,
        account_scope="primary",
        market_tz=MARKET_TZ,
        point=0.01,
    )
    assert result.detections, "the day must produce detections to store"
    client = InMemoryFirestore()
    for detection in result.detections:
        DetectionRepository(client).upsert(detection)
    EvaluationRepository(client).upsert_many(result.evaluations)
    return client, result


def test_stored_detections_are_counted_per_agent(tmp_path, stored) -> None:
    client, result = stored
    check = verifier(tmp_path, client=client)
    outcome = check.check_detections_stored()
    assert outcome.status is Status.PASS
    assert f"{len(result.detections)} detections" in outcome.detail
    assert "ema_cross=" in outcome.detail


def test_a_session_that_stored_nothing_fails(tmp_path) -> None:
    """Even with every other check green: there is nothing to have been right about."""
    result = verifier(tmp_path, client=InMemoryFirestore()).check_detections_stored()
    assert result.status is Status.FAIL
    assert "0 detections" in result.detail


def test_detections_without_evaluations_warn_rather_than_fail(tmp_path, day_candles) -> None:
    """Horizons resolve after the close, so a same-day verification legitimately has few."""
    from aureon.engine.analysis_engine import AnalysisEngine
    from aureon.storage.detection_repository import DetectionRepository
    from tests.conftest import cross_agent

    client = InMemoryFirestore()
    detections = AnalysisEngine(
        [cross_agent()], account_scope="primary", market_tz=MARKET_TZ
    ).feed(day_candles)
    assert detections
    for detection in detections:
        DetectionRepository(client).upsert(detection)

    result = verifier(tmp_path, client=client).check_detections_stored()
    assert result.status is Status.WARN
    assert "carry no evaluation" in result.detail


def test_the_outcome_block_comes_from_what_was_stored(tmp_path, stored) -> None:
    client, _result = stored
    check = verifier(tmp_path, client=client)
    check.run()
    outcomes = [b for b in check.blocks if b.title.startswith("Outcomes")]
    assert outcomes, "the evidence must carry the day's outcome table"
    assert outcomes[0].markdown
    assert "| session | horizon |" in outcomes[0].body
    assert "report_outcomes.py" in (outcomes[0].command or "")


# ── Outbox and heartbeat ─────────────────────────────────────────────────────


def test_a_drained_outbox_passes(tmp_path) -> None:
    result = verifier(tmp_path).check_outbox_drained()
    assert result.status is Status.PASS
    assert "0 pending" in result.detail


def test_pending_rows_make_every_count_a_lower_bound(tmp_path, day_candles) -> None:
    from aureon.engine.analysis_engine import AnalysisEngine
    from aureon.outbox.local_outbox import LocalOutbox
    from tests.conftest import cross_agent

    detections = AnalysisEngine(
        [cross_agent()], account_scope="primary", market_tz=MARKET_TZ
    ).feed(day_candles)
    with LocalOutbox(tmp_path / "outbox.db") as outbox:
        outbox.enqueue(detections[0])

    result = verifier(tmp_path).check_outbox_drained()
    assert result.status is Status.FAIL
    assert "lower bound" in (result.remedy or "")


def _with_heartbeat(tmp_path, day_candles, beat_at: datetime | None):
    from aureon.storage.system_state_repository import HeartbeatRepository

    write_archive(tmp_path / "archive", day_candles)
    client = InMemoryFirestore()
    if beat_at is not None:
        HeartbeatRepository(client, min_interval_seconds=0.0).beat(
            paths.SERVICE_OBSERVER, force=True, now=beat_at
        )
    check = verifier(tmp_path, client=client)
    check.check_archive()
    return check.check_observer_ran_to_the_close()


def test_a_heartbeat_at_the_close_passes(tmp_path, day_candles) -> None:
    last = day_candles[-1].close_time
    assert _with_heartbeat(tmp_path, day_candles, last).status is Status.PASS


def test_an_observer_that_died_at_lunchtime_fails(tmp_path, day_candles) -> None:
    """The failure with no other trace: the archive simply stops and looks normal."""
    early = day_candles[-1].close_time - timedelta(hours=3)
    result = _with_heartbeat(tmp_path, day_candles, early)
    assert result.status is Status.FAIL
    assert "stopped beating before its last candle" in result.detail


def test_a_heartbeat_within_tolerance_still_passes(tmp_path, day_candles) -> None:
    slack = day_candles[-1].close_time - timedelta(
        minutes=5 * HEARTBEAT_TOLERANCE_CANDLES
    )
    assert _with_heartbeat(tmp_path, day_candles, slack).status is Status.PASS


def test_a_missing_heartbeat_cannot_rule_an_early_death_in_or_out(
    tmp_path, day_candles
) -> None:
    result = _with_heartbeat(tmp_path, day_candles, None)
    assert result.status is Status.WARN
    assert "cannot be ruled out" in (result.remedy or "")


# ── The whole run ────────────────────────────────────────────────────────────


def test_a_clean_session_verifies(tmp_path, day_candles, stored) -> None:
    client, _result = stored
    write_archive(tmp_path / "archive", day_candles)
    from aureon.storage.system_state_repository import HeartbeatRepository

    HeartbeatRepository(client, min_interval_seconds=0.0).beat(
        paths.SERVICE_OBSERVER, force=True, now=day_candles[-1].close_time
    )
    check = verifier(tmp_path, client=client, comparison=Comparison(0, "IDENTICAL"))
    report = check.run()

    assert [r.name for r in report.results] == [
        "archive",
        "archive_gaps",
        "live_vs_replay",
        "detections_stored",
        "outbox_drained",
        "observer_ran_to_the_close",
    ]
    assert report.ok, [r.detail for r in report.failures]
    assert report.exit_code == 0
    assert "SESSION VERIFIED" in report.summary(
        ready="SESSION VERIFIED", not_ready="SESSION NOT VERIFIED"
    )


def test_a_session_with_no_archive_verifies_nothing_and_says_so(tmp_path) -> None:
    report = verifier(tmp_path).run()
    assert report.exit_code == 1
    assert report.get("archive").status is Status.FAIL
    assert report.get("live_vs_replay").status is Status.SKIP
    assert "checks not run" in report.summary()

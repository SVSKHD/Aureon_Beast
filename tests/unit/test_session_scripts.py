"""The two session scripts: the gate, and what survives a session going wrong (P-3).

``session_run.py`` has one job beyond printing: it must not start a session that preflight
says will produce nothing, and it must leave a record either way. Both are tested with an
injected preflight and an injected observer, because the interesting cases are a FAILED
preflight and an observer that raises -- neither of which a real run offers on demand.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from aureon.services.checks import CheckReport, CheckResult, Status
from aureon.services.session_evidence import (
    AFTER_MARKER,
    evidence_path,
    split_at_marker,
)
from scripts import session_run, session_verify

NOW = datetime(2026, 9, 16, 23, 30, tzinfo=UTC)
MARKET_DATE = "2026-09-17"  # NOW is already the 17th in Athens
#: The scripts default to the first configured symbol, and the evidence file is named
#: after it (9A): two symbols verified on one date are two records, not one overwritten.
SYMBOL = "XAUUSD"


class FakePreflight:
    def __init__(self, report: CheckReport) -> None:
        self._report = report
        self.runs = 0

    def run(self) -> CheckReport:
        self.runs += 1
        return self._report


def passing_report() -> CheckReport:
    return CheckReport(
        results=[
            CheckResult("config", Status.PASS, "account_scope=primary"),
            CheckResult(
                "mt5_init", Status.PASS, "connected to MetaTrader 5 build 4410"
            ),
            CheckResult(
                "mt5_account", Status.PASS, "login=5150 server=BrokerX-Demo currency=USD"
            ),
        ]
    )


def failing_report() -> CheckReport:
    return CheckReport(
        results=[
            CheckResult("config", Status.PASS, "account_scope=primary"),
            CheckResult("mt5_init", Status.FAIL, "MT5 initialize failed"),
        ]
    )


def run_session(tmp_path: Path, *argv: str, report=None, observer=None):
    calls: list[str] = []

    def default_observer() -> int:
        calls.append("observer")
        return 0

    code = session_run.main(
        ["--market-date", MARKET_DATE, *argv],
        preflight_factory=(lambda: FakePreflight(report or passing_report())),
        observer=observer or default_observer,
        evidence_root=tmp_path,
        now=lambda: NOW,
    )
    return code, calls, evidence_path(MARKET_DATE, SYMBOL, root=tmp_path)


# ── The gate ─────────────────────────────────────────────────────────────────


def test_a_failed_preflight_does_not_start_the_observer(tmp_path, capsys) -> None:
    code, calls, path = run_session(tmp_path, report=failing_report())
    assert code == 1
    assert calls == [], "a session that would produce nothing must not start"
    assert "Session not started" in path.read_text(encoding="utf-8")


def test_force_starts_anyway_and_records_that_it_did(tmp_path, capsys) -> None:
    """A gate with no override is a gate people avoid by not running it."""
    code, calls, path = run_session(tmp_path, "--force", report=failing_report())
    assert code == 0
    assert calls == ["observer"]
    text = path.read_text(encoding="utf-8")
    assert "Started with --force over a FAILED preflight" in text
    assert "MT5 initialize failed" in text, "and the failing row is still in the table"


def test_a_dry_run_writes_the_record_without_starting_anything(tmp_path, capsys) -> None:
    code, calls, path = run_session(tmp_path, "--dry-run")
    assert code == 0
    assert calls == []
    text = path.read_text(encoding="utf-8")
    assert "Dry run" in text
    assert "| observer started | — |" in text


# ── The record ───────────────────────────────────────────────────────────────


def test_the_record_is_written_before_the_observer_and_stamped_after(tmp_path) -> None:
    seen: dict[str, str] = {}
    path = evidence_path(MARKET_DATE, SYMBOL, root=tmp_path)

    def observer() -> int:
        # The document must already exist by the time the observer is running: a session
        # that crashes here is exactly when the record matters most.
        seen["during"] = path.read_text(encoding="utf-8")
        return 0

    code, _calls, _path = run_session(tmp_path, observer=observer)
    assert code == 0
    assert "| observer started | 2026-09-16T23:30:00+00:00 |" in seen["during"]
    assert "| observer stopped | — |" in seen["during"]

    after = path.read_text(encoding="utf-8")
    assert "| observer stopped | 2026-09-16T23:30:00+00:00 |" in after
    assert AFTER_MARKER in after


def test_a_crashing_observer_still_leaves_a_stopped_stamp(tmp_path) -> None:
    def observer() -> int:
        raise RuntimeError("the terminal went away")

    with pytest.raises(RuntimeError, match="terminal went away"):
        run_session(tmp_path, observer=observer)

    text = evidence_path(MARKET_DATE, SYMBOL, root=tmp_path).read_text(encoding="utf-8")
    assert "| observer stopped | 2026-09-16T23:30:00+00:00 |" in text


def test_the_terminal_build_and_server_come_from_preflights_own_rows(tmp_path) -> None:
    """Not from a second connection, which could report a different terminal."""
    _code, _calls, path = run_session(tmp_path, "--dry-run")
    text = path.read_text(encoding="utf-8")
    assert "build 4410" in text
    assert "| broker server | BrokerX-Demo |" in text


def test_the_market_date_defaults_to_the_broker_clock(tmp_path) -> None:
    """23:30 UTC is already tomorrow in Athens."""
    code = session_run.main(
        ["--dry-run"],
        preflight_factory=(lambda: FakePreflight(passing_report())),
        observer=(lambda: 0),
        evidence_root=tmp_path,
        now=lambda: NOW,
    )
    assert code == 0
    assert evidence_path("2026-09-17", SYMBOL, root=tmp_path).exists()
    assert not evidence_path("2026-09-16", SYMBOL, root=tmp_path).exists()


# ── Verification ─────────────────────────────────────────────────────────────


class FakeVerifier:
    def __init__(self, report: CheckReport) -> None:
        self._report = report
        self.blocks: list = []

    def run(self) -> CheckReport:
        from aureon.services.session_evidence import Block

        self.blocks.append(Block(title="Live vs replay", body="IDENTICAL"))
        return self._report


def verified(tmp_path, *argv: str, report: CheckReport):
    return session_verify.main(
        [MARKET_DATE, *argv],
        verifier_factory=(lambda: FakeVerifier(report)),
        evidence_root=tmp_path,
        now=lambda: NOW,
    )


def test_verification_fills_the_second_half_and_keeps_the_first(tmp_path) -> None:
    run_session(tmp_path, "--dry-run")
    before = evidence_path(MARKET_DATE, SYMBOL, root=tmp_path).read_text(encoding="utf-8")
    head, _tail = split_at_marker(before)

    code = verified(
        tmp_path,
        report=CheckReport(results=[CheckResult("archive", Status.PASS, "288 candles")]),
    )
    assert code == 0
    after = evidence_path(MARKET_DATE, SYMBOL, root=tmp_path).read_text(encoding="utf-8")
    assert after.startswith(head)
    assert "SESSION VERIFIED" in after
    assert "### Live vs replay" in after


def test_a_failed_check_exits_non_zero(tmp_path) -> None:
    run_session(tmp_path, "--dry-run")
    code = verified(
        tmp_path,
        report=CheckReport(
            results=[CheckResult("archive", Status.FAIL, "does not exist")]
        ),
    )
    assert code == 1
    text = evidence_path(MARKET_DATE, SYMBOL, root=tmp_path).read_text(encoding="utf-8")
    assert "SESSION NOT VERIFIED" in text


def test_verifying_twice_is_safe(tmp_path) -> None:
    """Re-running the day after is the point: PENDING horizons have resolved."""
    run_session(tmp_path, "--dry-run")
    report = CheckReport(results=[CheckResult("archive", Status.PASS, "288 candles")])
    verified(tmp_path, report=report)
    first = evidence_path(MARKET_DATE, SYMBOL, root=tmp_path).read_text(encoding="utf-8")
    verified(tmp_path, report=report)
    second = evidence_path(MARKET_DATE, SYMBOL, root=tmp_path).read_text(encoding="utf-8")
    assert first == second, "a second verification of the same facts must be idempotent"


def test_an_unbracketed_session_gets_a_document_that_admits_it(tmp_path, capsys) -> None:
    code = verified(
        tmp_path,
        report=CheckReport(results=[CheckResult("archive", Status.PASS, "288 candles")]),
    )
    assert code == 0
    text = evidence_path(MARKET_DATE, SYMBOL, root=tmp_path).read_text(encoding="utf-8")
    assert "No pre-session record" in text
    assert "SESSION VERIFIED" in text
    assert "had no pre-session record" in capsys.readouterr().err


def test_no_write_leaves_the_file_alone(tmp_path) -> None:
    run_session(tmp_path, "--dry-run")
    path = evidence_path(MARKET_DATE, SYMBOL, root=tmp_path)
    before = path.read_text(encoding="utf-8")
    code = verified(
        tmp_path,
        "--no-write",
        report=CheckReport(
            results=[CheckResult("archive", Status.FAIL, "does not exist")]
        ),
    )
    assert code == 1
    assert path.read_text(encoding="utf-8") == before


# ── 9A: one record per symbol ─────────────────────────────────────────────────


def test_each_symbol_gets_its_own_evidence_file(tmp_path) -> None:
    """9A. Two symbols verified on one date are two records.

    Every check inside is about one symbol -- its detections, its archive, its
    live-vs-replay comparison, its rule -- so a shared file would mean the second
    verification silently replacing the first's verdict, under a header naming one of them.
    """
    gold = evidence_path(MARKET_DATE, "XAUUSD", root=tmp_path)
    silver = evidence_path(MARKET_DATE, "XAGUSD", root=tmp_path)
    assert gold != silver
    assert gold.name == f"session_{MARKET_DATE}_XAUUSD.md"
    # Lower case in, upper case out: the file name matches the symbol as Aureon stores it.
    assert evidence_path(MARKET_DATE, "xagusd", root=tmp_path) == silver
    # And the pre-9A name is still addressable, for a file written before the split.
    assert evidence_path(MARKET_DATE, root=tmp_path).name == f"session_{MARKET_DATE}.md"


def test_a_session_run_writes_the_file_named_after_its_symbol(tmp_path) -> None:
    code, _calls, path = run_session(tmp_path, "--dry-run")
    assert code == 0
    assert path.exists()
    assert path.name.endswith("_XAUUSD.md")

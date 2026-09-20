"""The Evidence column says what is on disk, and cannot be talked out of it (P-5).

The whole value of a derived column is that it rots in the safe direction. A phase whose
evidence was produced and later removed must go back to `⬜ missing`, and a phase whose
generated document no longer contains the section its gate asked for must stop counting
even though the file is still there. Both are tested by taking the evidence away.

The committed document is also checked for staleness here, so an artefact that appears or
disappears without the column being regenerated is a failing test rather than a claim
nobody re-read.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.update_phases import (
    CATALOGUE,
    Artefact,
    PhaseEvidence,
    main,
    render_cell,
    rewrite,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
PHASES = REPO_ROOT / "docs" / "PHASES.md"

TABLE = """# Phases

| Phase | Scope | Gate ("Done when") | Status |
|---|---|---|---|
| 0 | Repo scaffolding, standing rules, boundary guards | guards green | done |
| 1 | models and contracts | contracts clean | done |
| 2 | Observer | baseline recorded | partial |
| 3 | Evaluation | outcomes counted | done |
| 4 | Execution | scenarios pass | partial |
| 5 | Positions | demo | partial |
| 6 | Discord | live | partial |
| 7 | Reviews | reconciled | done |
| 8 | Vue | dashboard | not started |
| 9A | Two symbols | both run in one observer | partial |
| — | **Corrections slice** (identity, EMA) | suite green | partial |
| — | **Defect register D-1…D-15** | each item green | closed |

Trailing prose.
"""


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    """A repository with every artefact the catalogue looks for."""
    docs = tmp_path / "docs"
    (docs / "evidence").mkdir(parents=True)
    (tmp_path / "tests" / "boundary").mkdir(parents=True)
    (tmp_path / "tests" / "boundary" / "test_architecture_boundaries.py").write_text(
        "def test_one():\n    pass\n\n\ndef test_two():\n    pass\n", encoding="utf-8"
    )
    (docs / "CONTRACTS.md").write_text("# Contracts\n", encoding="utf-8")
    (docs / "PHASE2_BASELINE.md").write_text(
        "## Detection outcomes — `XAU_OUTCOME_V2`\n\n"
        "Reconciled: every figure above agrees.\n",
        encoding="utf-8",
    )
    (docs / "PHASE1_DECISIONS.md").write_text("| 120 | x | y |\n", encoding="utf-8")
    (docs / "evidence" / "session_2026-09-16.md").write_text(
        "SESSION VERIFIED — 6 pass\n", encoding="utf-8"
    )
    (docs / "evidence" / "demo_drills_2026-09-16.md").write_text(
        "| broker | `mt5` |\n", encoding="utf-8"
    )
    (docs / "evidence" / "discord_2026-09-16.md").write_text("ok\n", encoding="utf-8")
    (docs / "PHASES.md").write_text(TABLE, encoding="utf-8")
    return tmp_path


def evidence_cells(text: str) -> dict[str, str]:
    """``{phase: evidence cell}`` from a rendered table."""
    cells: dict[str, str] = {}
    lines = [line for line in text.splitlines() if line.startswith("|")]
    header = [c.strip() for c in lines[0].strip("|").split("|")]
    at = header.index("Evidence")
    for line in lines[2:]:
        row = [c.strip() for c in line.strip("|").split("|")]
        key = row[0] if row[0] != "—" else row[1][:20]
        cells[key] = row[at]
    return cells


# ── With everything present ──────────────────────────────────────────────────


def test_the_column_is_added_when_it_is_absent(fake_repo) -> None:
    updated = rewrite(TABLE, root=fake_repo, docs=fake_repo / "docs")
    assert '| Phase | Scope | Gate ("Done when") | Status | Evidence |' in updated
    assert "| --- | --- | --- | --- | --- |" in updated
    assert "Trailing prose." in updated, "the rest of the document is untouched"


def test_every_phase_reports_its_artefact(fake_repo) -> None:
    cells = evidence_cells(rewrite(TABLE, root=fake_repo, docs=fake_repo / "docs"))
    assert cells["0"] == "✅ 2 boundary guards", "the COUNT, not just the file"
    assert cells["1"] == "✅ [CONTRACTS.md](CONTRACTS.md)"
    assert "session_2026-09-16.md" in cells["2"]
    assert cells["3"] == "✅ [XAU_OUTCOME_V2 outcomes](PHASE2_BASELINE.md)"
    assert "demo_drills_2026-09-16.md" in cells["4"]
    assert cells["7"] == "✅ [review reconciliation](PHASE2_BASELINE.md)"
    assert cells["8"] == "—", "a phase with no gate artefact says so"


def test_the_label_is_the_link_text_not_the_filename(fake_repo) -> None:
    """Three phases are closed by different sections of one document, and three identical
    links would say nothing about which."""
    cells = evidence_cells(rewrite(TABLE, root=fake_repo, docs=fake_repo / "docs"))
    assert cells["3"] != cells["7"]
    assert "PHASE2_BASELINE.md" in cells["3"] and "PHASE2_BASELINE.md" in cells["7"]


def test_the_two_unnumbered_rows_are_told_apart_by_scope(fake_repo) -> None:
    cells = evidence_cells(rewrite(TABLE, root=fake_repo, docs=fake_repo / "docs"))
    assert "session_2026-09-16.md" in cells["**Corrections slice*"]
    assert "PHASE1_DECISIONS.md" in cells["**Defect register D-"]


def test_rewriting_twice_changes_nothing(fake_repo) -> None:
    once = rewrite(TABLE, root=fake_repo, docs=fake_repo / "docs")
    twice = rewrite(once, root=fake_repo, docs=fake_repo / "docs")
    assert once == twice


# ── With the evidence taken away ─────────────────────────────────────────────


def test_a_removed_artefact_goes_back_to_missing(fake_repo) -> None:
    """The direction that matters: a phase whose evidence is gone must not stay green."""
    (fake_repo / "docs" / "evidence" / "session_2026-09-16.md").unlink()
    cells = evidence_cells(rewrite(TABLE, root=fake_repo, docs=fake_repo / "docs"))
    assert "⬜ missing: verified real session" in cells["2"]
    assert "session_run.py" in cells["2"], "and it names what would produce it"


def test_a_document_that_lost_its_section_stops_counting(fake_repo) -> None:
    """The file is still there. That is exactly why content is checked and not existence."""
    (fake_repo / "docs" / "PHASE2_BASELINE.md").write_text(
        "# Baseline\n\nnothing about outcomes here\n", encoding="utf-8"
    )
    cells = evidence_cells(rewrite(TABLE, root=fake_repo, docs=fake_repo / "docs"))
    assert cells["3"].startswith("⬜ missing")
    assert cells["7"].startswith("⬜ missing")
    assert "PHASE2_BASELINE.md" in cells["2"], "the file itself is still evidence for 2"


def test_a_drill_run_against_the_fake_broker_is_not_demo_evidence(fake_repo) -> None:
    """A rehearsal proves the drills are right. It does not close phase 4."""
    (fake_repo / "docs" / "evidence" / "demo_drills_2026-09-16.md").write_text(
        "| broker | `fake` |\n", encoding="utf-8"
    )
    cells = evidence_cells(rewrite(TABLE, root=fake_repo, docs=fake_repo / "docs"))
    assert cells["4"].startswith("⬜ missing")


def test_the_status_column_is_never_touched(fake_repo) -> None:
    """Whether a partial pass counts is a judgement, not a file listing."""
    updated = rewrite(TABLE, root=fake_repo, docs=fake_repo / "docs")
    for line in updated.splitlines():
        if line.startswith("| 4 |"):
            assert "partial" in line
        if line.startswith("| 8 |"):
            assert "not started" in line


# ── Failure modes of the tool itself ─────────────────────────────────────────


def test_a_table_missing_a_catalogued_phase_is_refused(fake_repo) -> None:
    """Otherwise the unwritten cell keeps whatever it said before, unnoticed."""
    without_seven = "\n".join(
        line for line in TABLE.splitlines() if not line.startswith("| 7 |")
    )
    with pytest.raises(ValueError, match="no row in docs/PHASES.md matched: 7"):
        rewrite(without_seven, root=fake_repo, docs=fake_repo / "docs")


def test_a_document_with_no_table_is_refused(fake_repo) -> None:
    with pytest.raises(ValueError, match="no phase table"):
        rewrite("# Phases\n\nprose only\n", root=fake_repo, docs=fake_repo / "docs")


def test_an_empty_artefact_list_renders_a_dash(fake_repo) -> None:
    entry = PhaseEvidence("9", None, ())
    assert render_cell(entry, root=fake_repo, docs=fake_repo / "docs") == "—"


def test_several_dated_files_are_listed_by_name(fake_repo) -> None:
    evidence = fake_repo / "docs" / "evidence"
    for day in ("17", "18"):
        (evidence / f"session_2026-09-{day}.md").write_text(
            "SESSION VERIFIED\n", encoding="utf-8"
        )
    entry = PhaseEvidence(
        "2",
        None,
        (
            Artefact(
                label="verified real session",
                path="evidence/session_*.md",
                must_contain="SESSION VERIFIED",
            ),
        ),
    )
    cell = render_cell(entry, root=fake_repo, docs=fake_repo / "docs")
    assert "session_2026-09-18.md" in cell
    assert cell.count("](") == 3, "the most recent few, not one link"


# ── The committed document ───────────────────────────────────────────────────


def test_the_committed_evidence_column_is_up_to_date() -> None:
    assert main(["--check"]) == 0, (
        "docs/PHASES.md Evidence column is stale. Run "
        "`python scripts/update_phases.py` and commit the result."
    )


def test_the_committed_table_reports_what_is_genuinely_missing() -> None:
    """A guard against the column quietly going green: nothing has run against a real
    terminal or a demo account in this repository, and the table must keep saying so."""
    cells = evidence_cells(PHASES.read_text(encoding="utf-8"))
    assert cells["2"].count("⬜") == 1, cells["2"]
    assert cells["4"].startswith("⬜"), cells["4"]
    assert cells["6"].startswith("⬜"), cells["6"]
    # 9A's own real-session leg: nothing has run against a terminal for silver either.
    assert "⬜" in cells["9A"], cells["9A"]
    assert len(CATALOGUE) == 12
